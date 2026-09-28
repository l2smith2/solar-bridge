"""Web app: setup mode, settings API, reloads, add-ons — driven over real HTTP."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import stat
import struct
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from solar_bridge.app import addons
from solar_bridge.app.web import MASK, App


class Running:
    """An App running in the background, with a helper for HTTP calls."""

    def __init__(self, app: App, port: int) -> None:
        self.app, self.port = app, port
        self.auth: str | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> Running:
        self._task = asyncio.create_task(self.app.run(self._stop))
        await self.wait_up()
        return self

    async def __aexit__(self, *exc) -> None:
        self._stop.set()
        await asyncio.wait_for(self._task, 10)

    async def wait_up(self, port: int | None = None) -> None:
        for _ in range(100):
            await asyncio.sleep(0.05)
            try:
                with socket.create_connection(("127.0.0.1", port or self.port), timeout=0.2):
                    return
            except OSError:
                pass
        raise AssertionError("server did not start")

    async def call(self, method: str, path: str, body=None, port: int | None = None):
        headers = {"Authorization": self.auth} if self.auth else {}
        async with aiohttp.ClientSession() as session, session.request(
            method, f"http://127.0.0.1:{port or self.port}{path}", json=body, headers=headers
        ) as resp:
            text = await resp.text()
            try:
                return resp.status, json.loads(text)
            except ValueError:
                return resp.status, text

    async def save(self, config: dict) -> None:
        status, body = await self.call("POST", "/api/config", config)
        assert status == 200, body
        await asyncio.sleep(0.6)  # reload happens right after the response
        await self.wait_up(config.get("http_port"))


@pytest.fixture
async def device(port_factory):
    """A fake device with a JSON API."""
    port = port_factory()
    readings = {"grid": -1500.0, "solar": 4000.0, "battery": 1200.0, "soc": 64.0}

    async def handler(request: web.Request) -> web.Response:
        return web.json_response(readings)

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    yield f"http://127.0.0.1:{port}/"
    await runner.cleanup()


def _config(url: str, http_port: int, modbus_port: int, **extra) -> dict:
    return {
        "name": "e2e", "display_name": "MyHome", "http_port": http_port, "modbus_port": modbus_port,
        "wattpilot": False, "update_interval": 1,
        "sources": {"dev": {"type": "http_json", "url": url, "password": "secret",
                            "fields": {"grid": "grid", "solar": "solar", "battery": "battery", "soc": "soc"}}},
        "values": {"grid": "dev.grid", "pv": "dev.solar", "battery": "dev.battery", "soc": "dev.soc"},
        "meters": [{"name": "grid", "unit_id": 240, "value": "grid"},
                   {"name": "ac-battery", "unit_id": 241, "value": "battery", "role": "generator"}],
        **extra,
    }


async def test_setup_from_scratch_through_the_web_page(tmp_path: Path, port_factory, device) -> None:
    setup_port, http_port, modbus_port = port_factory(), port_factory(), port_factory()
    async with Running(App(tmp_path, None, setup_port), setup_port) as r:
        # Unconfigured: the pages and API answer, nothing else runs
        status, state = await r.call("GET", "/api/state")
        assert state["configured"] is False
        assert (await r.call("GET", "/settings"))[0] == 200
        status, body = await r.call("GET", "/api/config")
        assert body["config"] == {} and body["managed_by_file"] is None

        # Invalid settings are refused with a readable reason
        status, body = await r.call("POST", "/api/config", {"sources": {}})
        assert status == 400 and "add at least one device" in body["error"]

        # Test a device before saving
        status, body = await r.call("POST", "/api/test-source", {"name": "dev", "config": {
            "type": "http_json", "url": device, "fields": {"grid": "grid"}}})
        assert body == {"ok": True, "error": None, "values": {"grid": -1500.0}}

        # Save → the bridge restarts on the configured port
        await r.save(_config(device, http_port, modbus_port))
        saved = tmp_path / "config.json"
        assert stat.S_IMODE(os.stat(saved).st_mode) == 0o600  # holds device passwords
        r.port = http_port
        await asyncio.sleep(1.2)
        status, flow = await r.call("GET", "/solar_api/v1/GetPowerFlowRealtimeData.fcgi")
        site = flow["Body"]["Data"]["Site"]
        assert (site["P_Grid"], site["P_PV"], site["P_Akku"], site["P_Load"]) == (-1500.0, 4000.0, 1200.0, -3700.0)
        status, state = await r.call("GET", "/api/state")
        assert state["configured"] and state["values"]["load"] == 3700.0 and state["sources"]["dev"]["ok"]

        reader, writer = await asyncio.open_connection("127.0.0.1", modbus_port)
        for unit, expected in ((240, -1500.0), (241, -1200.0)):
            writer.write(struct.pack(">HHHBBHH", 1, 0, 6, unit, 3, 40097, 2))
            await writer.drain()
            reply = await asyncio.wait_for(reader.readexactly(13), 2)
            assert struct.unpack(">f", reply[9:13])[0] == expected

        # Secrets never go back to the browser, and survive a save that sends the mask
        status, body = await r.call("GET", "/api/config")
        assert body["config"]["sources"]["dev"]["password"] == MASK
        changed = body["config"] | {"display_name": "Renamed"}
        await r.save(changed)
        stored = json.loads(saved.read_text())
        assert stored["sources"]["dev"]["password"] == "secret"
        assert stored["display_name"] == "Renamed"
        writer.close()  # the Modbus server was restarted under this client

    energy = json.loads((tmp_path / "energy.json").read_text())
    assert set(energy["meters"]) == {"grid", "ac-battery"}


async def test_settings_password(tmp_path: Path, port_factory, device) -> None:
    port = port_factory()
    async with Running(App(tmp_path, None, port), port) as r:
        await r.save(_config(device, port, port_factory(), settings_password="pässwörd"))
        assert "settings_password" not in json.loads((tmp_path / "config.json").read_text())

        assert (await r.call("GET", "/api/config"))[0] == 401
        assert (await r.call("POST", "/api/test-source", {"name": "x", "config": {}}))[0] == 401
        assert (await r.call("POST", "/api/addons/tesla/install"))[0] == 401
        assert (await r.call("GET", "/api/state"))[0] == 200  # the power-flow page stays public

        r.auth = "Basic " + base64.b64encode("admin:wrong".encode()).decode()
        assert (await r.call("GET", "/api/config"))[0] == 401
        r.auth = "Basic " + base64.b64encode("admin:pässwörd".encode()).decode()
        status, body = await r.call("GET", "/api/config")
        assert status == 200 and body["has_settings_password"] is True
        assert "settings_password_hash" not in body["config"]

        # Saving without a new password keeps the old one; removing it works
        await r.save(body["config"])
        assert (await r.call("GET", "/api/config"))[1]["has_settings_password"] is True
        await r.save(body["config"] | {"remove_settings_password": True})
        r.auth = None
        assert (await r.call("GET", "/api/config"))[0] == 200


async def test_settings_file_is_read_only_in_the_page(tmp_path: Path, port_factory, device) -> None:
    port = port_factory()
    file = tmp_path / "settings.json"
    file.write_text(json.dumps(_config(device, port, port_factory())))
    async with Running(App(tmp_path / "data", file, port), port) as r:
        status, body = await r.call("GET", "/api/config")
        assert body["managed_by_file"] == str(file)
        assert (await r.call("POST", "/api/config", body["config"]))[0] == 409


async def test_busy_port_falls_back_to_setup_page(tmp_path: Path, port_factory, device) -> None:
    setup_port = port_factory()
    with socket.socket() as blocker:
        blocker.bind(("0.0.0.0", 0))
        blocker.listen()
        busy = blocker.getsockname()[1]
        (tmp_path / "config.json").write_text(json.dumps(_config(device, busy, port_factory())))
        async with Running(App(tmp_path, None, setup_port), setup_port) as r:
            status, state = await r.call("GET", "/api/state")
            assert state["configured"] is False
            assert any(f"port {busy}" in p for p in state["problems"])


async def test_broken_settings_file_still_shows_the_page(tmp_path: Path, port_factory) -> None:
    port = port_factory()
    (tmp_path / "config.json").write_text('{"sources": {}}')
    async with Running(App(tmp_path, None, port), port) as r:
        status, state = await r.call("GET", "/api/state")
        assert any("Configuration problem" in p for p in state["problems"])


async def test_addon_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Installs into <data_dir>/addons and makes the module importable (pip is faked)."""
    calls = []

    class FakeProcess:
        def __init__(self, target: Path) -> None:
            (target / "fake_addon_mod").mkdir(parents=True)
            (target / "fake_addon_mod" / "__init__.py").write_text("VALUE = 42\n")
            self.stdout = self._lines()

        async def _lines(self):
            yield b"Successfully installed fake-addon\n"

        async def wait(self) -> int:
            return 0

    async def fake_exec(*cmd, **kwargs):
        calls.append(cmd)
        return FakeProcess(Path(cmd[cmd.index("--target") + 1]))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setitem(addons.ADDONS, "fake", addons.Addon("fake", "Fake", "fake-addon", "fake_addon_mod"))
    assert not addons.installed("fake")
    assert await addons.install("fake", tmp_path)
    assert "fake-addon" in calls[0] and "--target" in calls[0]
    assert addons.installed("fake")
    assert addons.status()["fake"]["log"] == ["Successfully installed fake-addon"]
    import fake_addon_mod  # noqa: F401 — importable without a restart
