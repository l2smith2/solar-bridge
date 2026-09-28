"""Standalone app: config, sources, bridge."""
from __future__ import annotations

import asyncio
import struct
import sys
import types
from pathlib import Path

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
    assert cfg.values["battery"].parts == [("powerwall", "battery")]
    assert [(m.unit_id, m.role) for m in cfg.meters] == [(240, "grid"), (241, "generator")]
    assert cfg.serial == "MyHome"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": "my bridge"}, "network name"),
        ({"values": {"pv": "s.g"}}, "grid power is required"),
        ({"values": {"grid": "nope.g"}}, "no device called"),
        ({"values": {"grid": "s.g", "voltage": "s.g"}}, "unknown value"),
        ({"sources": {"s": {"type": "carrier-pigeon"}}}, "type must be one of"),
        ({"sources": {}}, "add at least one device"),
        ({"sources": {"bad name": {"type": "mqtt"}}}, "letters, digits"),
        ({"grid": {"breaker_amps": "lots"}}, "expected a number"),
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
    bridge = Bridge(cfg, tmp_path)
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
    bridge = Bridge(parse(_config()), tmp_path)
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


def test_tesla_without_addon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pypowerwall", None)  # import fails
    from solar_bridge.app.sources.tesla import TeslaSource

    source = TeslaSource("pw", {"type": "tesla", "host": "x"})
    asyncio.run(source.refresh())
    assert "install it from the settings page" in source.error


def test_values_can_add_up_parts(tmp_path: Path) -> None:
    cfg = parse(_config(values={"grid": ["s.l1", "s.l2", "s.l3"], "pv": {"from": "s.pv", "scale": 1000}}))
    bridge = Bridge(cfg, tmp_path)
    src = bridge.sources["s"]
    src.set("l1", 100.0)
    src.set("l2", 200.0)
    src.set("pv", 1.5)
    bridge.compute()
    assert bridge.values["grid"] == 300.0  # a missing phase doesn't blank the total
    assert bridge.values["pv"] == 1500.0


async def test_http_json_one_bad_field_keeps_the_rest(free_port: int) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"grid": 50, "pv": None})

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", free_port).start()
    source = HttpJsonSource("d", {"type": "http_json", "url": f"http://127.0.0.1:{free_port}/",
                                  "fields": {"grid": "grid", "pv": "pv", "load": "no.such.path"}})
    await source.start()
    try:
        await source.refresh()
    finally:
        await source.stop()
        await runner.cleanup()
    assert source.get("grid") == 50.0
    assert source.get("pv") is None  # JSON null (e.g. an inverter asleep) is not an error
    assert source.error and source.error.startswith("load:")


async def test_modbus_one_bad_register_keeps_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    source = ModbusSource("m", {"type": "modbus", "host": "h", "fields": {
        "a": {"address": 1, "type": "uint16"}, "b": {"address": 2, "type": "uint16"}}})

    async def fake_read(unit, address, count, input_registers=False):
        if address == 2:
            raise ConnectionError("Modbus exception 2")
        return struct.pack(">H", 7)

    monkeypatch.setattr(source._client, "read", fake_read)
    await source.refresh()
    assert source.get("a") == 7.0 and source.get("b") is None
    assert "b: Modbus exception 2" in source.error


def test_templates_build_valid_sources() -> None:
    from solar_bridge.app.sources import create_source
    from solar_bridge.app.templates import TEMPLATES

    def fill(obj, ask):  # as the settings page does: typed values or each field's default
        if isinstance(obj, str):
            for key, value in ask.items():
                obj = obj.replace("{" + key + "}", str(value))
            assert "{" not in obj, obj
            return obj
        if isinstance(obj, dict):
            return {k: fill(v, ask) for k, v in obj.items()}
        return obj

    for t in TEMPLATES:
        ask = {a["key"]: a.get("default", "x") for a in t.get("ask", [])} | {"host": "192.168.1.9"}
        src = fill(t["source"], ask)
        if not src.get("fields") and src["type"] != "tesla":
            src["fields"] = {"x": {"address": 1} if src["type"] == "modbus" else "x"}
        create_source(t["id"], src)  # raises if the template is malformed
        fields = {"grid", "solar", "battery", "load", "soc"} if src["type"] == "tesla" else set(src["fields"])
        for value, sug in t["values"].items():
            refs = sug["from"] if isinstance(sug, dict) else sug
            for ref in [refs] if isinstance(refs, str) else refs:
                assert ref in fields, (t["id"], value, ref)


def test_friendly_errors_for_blank_numbers() -> None:
    from solar_bridge.app.sources import create_source

    with pytest.raises(ValueError, match="unit must be a number"):
        create_source("m", {"type": "modbus", "host": "h", "unit": "", "fields": {"a": {"address": 1}}})
    with pytest.raises(ValueError, match="port must be a number"):
        create_source("q", {"type": "mqtt", "host": "h", "port": "abc", "fields": {"a": "t"}})
