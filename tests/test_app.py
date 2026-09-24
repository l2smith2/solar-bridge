"""Standalone app: config, sources, bridge."""
from __future__ import annotations

import asyncio
import json
import struct
import sys
import types
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from solar_bridge.app.bridge import Bridge, meter_power
from solar_bridge.app.config import ConfigError, MeterConfig, load, parse
from solar_bridge.app.sources.http_json import HttpJsonSource
from solar_bridge.app.sources.modbus import ModbusSource, decode
from solar_bridge.app.sources.mqtt import MqttSource, parse_payload
from solar_bridge.core import ModbusTcpServer, SunSpecMeter

EXAMPLE = Path(__file__).parent.parent / "deploy" / "config.example.yaml"


def _config(**overrides) -> dict:
    raw = {
        "name": "test-bridge",
        "sources": {"s": {"type": "http_json", "url": "http://x", "fields": {"g": "a"}}},
        "values": {"grid": "s.g"},
    }
    raw.update(overrides)
    return raw


# ── config ──────────────────────────────────────────────────────────────────

def test_example_config_is_valid() -> None:
    cfg = load(EXAMPLE)
    assert cfg.values["battery"].field == "battery"
    assert [(m.unit_id, m.role) for m in cfg.meters] == [(240, "grid"), (241, "generator")]
    assert cfg.serial == "MyHome"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": "my bridge"}, "hostname"),
        ({"values": {"pv": "s.g"}}, "values.grid is required"),
        ({"values": {"grid": "nope.g"}}, "no source called"),
        ({"values": {"grid": "s.g", "voltage": "s.g"}}, "unknown value"),
        ({"sources": {"s": {"type": "carrier-pigeon"}}}, "type must be one of"),
        ({"meters": [{"unit_id": 240}, {"unit_id": 240}]}, "unique"),
        ({"meters": [{"value": "soc"}]}, "not a configured power value"),
        ({"grid": {"phases": 2}}, "phases must be 1 or 3"),
    ],
)
def test_config_errors(overrides: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(_config(**overrides))


def test_value_invert_and_scale() -> None:
    cfg = parse(_config(values={"grid": {"from": "s.g", "invert": True, "scale": 1000}}))
    assert (cfg.values["grid"].invert, cfg.values["grid"].scale) == (True, 1000.0)


# ── signs ───────────────────────────────────────────────────────────────────

def test_meter_power_signs() -> None:
    battery_meter = MeterConfig("bat", 241, "battery", role="generator")
    assert meter_power(battery_meter, 1200.0) == -1200.0  # discharging = towards the grid
    assert meter_power(battery_meter, -800.0) == 800.0  # charging = into the battery
    assert meter_power(MeterConfig("g", 240, "grid"), 500.0) == 500.0
    assert meter_power(MeterConfig("l", 242, "load", role="load"), 300.0) == 300.0
    assert meter_power(MeterConfig("g", 240, "grid", invert=True), 500.0) == -500.0
    assert meter_power(battery_meter, None) is None


def test_bridge_converts_to_fronius_and_balances_load(tmp_path: Path) -> None:
    cfg = parse(_config(
        data_dir=str(tmp_path),
        sources={"s": {"type": "http_json", "url": "http://x", "fields": {"g": "g"}}},
        values={"grid": "s.grid", "pv": "s.pv", "battery": "s.bat", "soc": "s.soc"},
        meters=[{"name": "bat", "unit_id": 241, "value": "battery", "role": "generator"}],
    ))
    bridge = Bridge(cfg)
    src = bridge.sources["s"]
    for field, value in {"grid": 200.0, "pv": 3000.0, "bat": 1000.0, "soc": 104.0}.items():
        src.set(field, value)
    bridge.compute()
    assert bridge.values["load"] == 4200.0  # grid + pv + battery
    assert bridge.data["P_Load"] == -4200.0  # Fronius: consumption negative
    assert bridge.data["P_Akku"] == 1000.0  # Fronius: + discharging
    assert bridge.data["SOC"] == 100.0  # clamped
    assert bridge.meter_data["bat"]["P_Grid"] == -1000.0


def test_stale_values_are_dropped(tmp_path: Path) -> None:
    bridge = Bridge(parse(_config(data_dir=str(tmp_path))))
    src = bridge.sources["s"]
    src.set("g", 100.0)
    assert src.get("g") == 100.0
    src.stale_after = -1
    assert src.get("g") is None


# ── sources ─────────────────────────────────────────────────────────────────

async def test_http_json_source(free_port: int) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"total_act_power": -1234.5, "emeters": [{"power": 2.5}]})

    app = web.Application()
    app.router.add_get("/status", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", free_port).start()
    source = HttpJsonSource("shelly", {
        "type": "http_json",
        "url": f"http://127.0.0.1:{free_port}/status",
        "fields": {"grid": "total_act_power", "pv": {"path": "emeters.0.power", "scale": 1000}},
    })
    await source.start()
    try:
        await source.refresh()
        assert (source.get("grid"), source.get("pv"), source.error) == (-1234.5, 2500.0, None)
    finally:
        await source.stop()
        await runner.cleanup()


async def test_http_json_source_reports_errors() -> None:
    source = HttpJsonSource("dead", {"type": "http_json", "url": "http://127.0.0.1:9/", "fields": {"g": "x"}})
    await source.start()
    await source.refresh()
    await source.stop()
    assert source.error and source.get("g") is None and not source.status()["ok"]


async def test_modbus_source_reads_a_sunspec_meter(free_port: int) -> None:
    """Round trip: our Modbus client reading our own Smart Meter IP emulation."""
    server = ModbusTcpServer(free_port, host="127.0.0.1")
    server.meters[240] = SunSpecMeter(lambda: {"P_Grid": -987.5}, "m", 240)
    await server.start()
    source = ModbusSource("meter", {
        "type": "modbus", "host": "127.0.0.1", "port": free_port, "unit": 240,
        "fields": {"grid": {"address": 40097, "type": "float32"}, "da": {"address": 40068, "type": "uint16"}},
    })
    try:
        await source.refresh()
        assert (source.get("grid"), source.get("da"), source.error) == (-987.5, 240.0, None)
    finally:
        await source.stop()
        await server.stop()


def test_modbus_decode() -> None:
    assert decode(struct.pack(">h", -5), "int16") == -5
    assert decode(struct.pack(">I", 70000), "uint32") == 70000
    raw = struct.pack(">i", 123456)
    assert decode(raw[2:] + raw[:2], "int32", swap_words=True) == 123456


def test_mqtt_payloads() -> None:
    assert parse_payload(b"1234.5", None) == 1234.5
    assert parse_payload('{"battery": {"instant_power": -300}}', "battery.instant_power") == -300
    source = MqttSource("b", {"type": "mqtt", "host": "h", "fields": {
        "grid": "home/grid", "bat": {"topic": "pw/agg", "path": "battery", "scale": -1}}})
    source.handle("home/grid", b"42")
    source.handle("pw/agg", b'{"battery": 100}')
    source.handle("other/topic", b"999")
    assert (source.get("grid"), source.get("bat")) == (42.0, -100.0)
    source.handle("home/grid", b"not a number")
    assert source.error


async def test_tesla_source(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}

    class FakePowerwall:
        def __init__(self, **kwargs):
            calls.update(kwargs)

        grid = staticmethod(lambda: 150.0)
        solar = staticmethod(lambda: 3200.0)
        battery = staticmethod(lambda: -1800.0)
        home = staticmethod(lambda: 1550.0)
        level = staticmethod(lambda scale=False: 87.0 if scale else 90.0)

    monkeypatch.setitem(sys.modules, "pypowerwall", types.SimpleNamespace(Powerwall=FakePowerwall))
    from solar_bridge.app.sources.tesla import TeslaSource

    source = TeslaSource("pw", {"type": "tesla", "host": "10.0.0.5", "password": "x", "options": {"timeout": 9}})
    await source.refresh()
    assert calls == {"host": "10.0.0.5", "password": "x", "timeout": 9}
    assert {f: source.get(f) for f in ("grid", "solar", "battery", "load", "soc")} == {
        "grid": 150.0, "solar": 3200.0, "battery": -1800.0, "load": 1550.0, "soc": 87.0}


# ── end to end ──────────────────────────────────────────────────────────────

async def test_bridge_end_to_end(tmp_path: Path, port_factory) -> None:
    feed_port, http_port, modbus_port = port_factory(), port_factory(), port_factory()
    readings = {"grid": -1500.0, "solar": 4000.0, "battery": 1200.0, "soc": 64.0}

    async def feed(request: web.Request) -> web.Response:
        return web.json_response(readings)

    app = web.Application()
    app.router.add_get("/", feed)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", feed_port).start()

    cfg = parse({
        "name": "e2e", "display_name": "MyHome", "http_port": http_port, "modbus_port": modbus_port,
        "data_dir": str(tmp_path), "wattpilot": False,
        "sources": {"dev": {"type": "http_json", "url": f"http://127.0.0.1:{feed_port}/",
                            "fields": {"grid": "grid", "solar": "solar", "battery": "battery", "soc": "soc"}}},
        "values": {"grid": "dev.grid", "pv": "dev.solar", "battery": "dev.battery", "soc": "dev.soc"},
        "meters": [{"name": "grid", "unit_id": 240, "value": "grid"},
                   {"name": "ac-battery", "unit_id": 241, "value": "battery", "role": "generator"}],
    })
    bridge = Bridge(cfg)
    stop = asyncio.Event()
    task = asyncio.create_task(bridge.run(stop))
    try:
        for _ in range(50):
            await asyncio.sleep(0.05)
            if bridge._http and bridge._modbus:
                break
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{http_port}/solar_api/v1/GetPowerFlowRealtimeData.fcgi") as r:
                site = (await r.json(content_type=None))["Body"]["Data"]["Site"]
            async with session.get(f"http://127.0.0.1:{http_port}/") as r:
                assert r.status == 200 and "power flow" in (await r.text()).lower()
            async with session.get(f"http://127.0.0.1:{http_port}/api/state") as r:
                state = await r.json()
        assert (site["P_Grid"], site["P_PV"], site["P_Akku"], site["P_Load"]) == (-1500.0, 4000.0, 1200.0, -3700.0)
        assert state["values"]["load"] == 3700.0
        assert state["sources"]["dev"]["ok"] is True
        assert "127.0.0.1" in state["clients"]  # our Solar API request was noticed

        reader, writer = await asyncio.open_connection("127.0.0.1", modbus_port)
        for unit, expected in ((240, -1500.0), (241, -1200.0)):
            writer.write(struct.pack(">HHHBBHH", 1, 0, 6, unit, 3, 40097, 2))
            await writer.drain()
            reply = await asyncio.wait_for(reader.readexactly(13), 2)
            assert struct.unpack(">f", reply[9:13])[0] == expected
        writer.close()
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)  # also proves shutdown doesn't hang with a client connected
        await runner.cleanup()

    saved = json.loads((tmp_path / "energy.json").read_text())
    assert set(saved["meters"]) == {"grid", "ac-battery"}
