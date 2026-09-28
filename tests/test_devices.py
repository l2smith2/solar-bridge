"""Brand templates end to end: realistic device answers → the values the Wattpilot gets."""
from __future__ import annotations

import asyncio
import json
import socket
import struct
from pathlib import Path

import pytest
from aiohttp import web

from solar_bridge.app.bridge import Bridge
from solar_bridge.app.config import parse
from solar_bridge.app.sources import SOURCE_TYPES, create_source
from solar_bridge.app.sources.modbus import ModbusSource
from solar_bridge.app.sources.sma_speedwire import SmaSpeedwireSource, parse_datagram
from solar_bridge.app.templates import TEMPLATES

SETTINGS_PAGE = Path(__file__).parent.parent / "src" / "solar_bridge" / "app" / "static" / "settings.html"


# ── helpers ─────────────────────────────────────────────────────────────────

def _from_template(tid: str, ask: dict) -> tuple[dict, dict]:
    """(source config, suggested values) as the settings page builds them."""
    t = next(t for t in TEMPLATES if t["id"] == tid)
    text = json.dumps(t["source"])
    for key, value in ask.items():
        text = text.replace("{" + key + "}", str(value))
    src = json.loads(text)
    for a in t.get("ask", []):  # asked for but not used in the template: a setting of its own
        if "{" + a["key"] + "}" not in json.dumps(t["source"]) and ask.get(a["key"]) not in (None, ""):
            src[a["key"]] = ask[a["key"]]

    def ref(v):
        return [ref(x) for x in v] if isinstance(v, list) else f"dev.{v}"

    values = {k: {"from": ref(v["from"]), "invert": v.get("invert", False)} if isinstance(v, dict) else ref(v)
              for k, v in t["values"].items()}
    return src, values


async def _bridge_values(tmp_path: Path, src: dict, values: dict) -> dict:
    bridge = Bridge(parse({"name": "t", "sources": {"dev": src}, "values": values}), tmp_path)
    source = bridge.sources["dev"]
    await source.start()
    try:
        await bridge.update()
    finally:
        await source.stop()
    assert source.error is None, source.error
    return bridge.values


def _s32(address: int, value: int) -> dict[int, int]:
    hi, lo = struct.unpack(">HH", struct.pack(">i", value))
    return {address: hi, address + 1: lo}


def _u32(address: int, value: int) -> dict[int, int]:
    hi, lo = struct.unpack(">HH", struct.pack(">I", value))
    return {address: hi, address + 1: lo}


S32_NAN, U32_NAN = -0x80000000, 0xFFFFFFFF


class FakeModbusDevice:
    """Answers register reads (FC3/FC4) for one unit ID from a map of 16-bit words."""

    def __init__(self, unit: int, registers: dict[int, int]) -> None:
        self.unit, self.registers, self.functions = unit, registers, set()
        self._server: asyncio.Server | None = None

    async def start(self, port: int) -> None:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", port)

    async def stop(self) -> None:
        assert self._server
        self._server.close()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                tid, _, length, unit = struct.unpack(">HHHB", await reader.readexactly(7))
                func, address, count = struct.unpack(">BHH", (await reader.readexactly(length - 1))[:5])
                self.functions.add(func)
                words = [self.registers.get(address + i) for i in range(count)]
                if unit != self.unit or func not in (3, 4) or None in words:
                    reply = bytes([func | 0x80, 2])  # illegal data address
                else:
                    reply = bytes([func, count * 2]) + struct.pack(f">{count}H", *words)
                writer.write(struct.pack(">HHHB", tid, 0, len(reply) + 1, unit) + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def modbus_device(free_port: int):
    devices = []

    async def make(unit: int, registers: dict[int, int]) -> FakeModbusDevice:
        device = FakeModbusDevice(unit, registers)
        await device.start(free_port)
        devices.append(device)
        return device

    make.port = free_port
    yield make
    for device in devices:
        await device.stop()


# ── Sigenergy ───────────────────────────────────────────────────────────────

async def test_sigenergy(tmp_path: Path, modbus_device) -> None:
    device = await modbus_device(247, {  # plant registers: W, and SOC in 0.1 %
        **_s32(30005, -1500),  # grid sensor: exporting 1.5 kW
        **_s32(30035, 4200),  # PV
        **_s32(30037, 2500),  # ESS: + charging
        30014: 815,
    })
    src, values = _from_template("sigenergy", {"host": "127.0.0.1"})
    src["port"] = modbus_device.port
    got = await _bridge_values(tmp_path, src, values)
    assert got == {"grid": -1500.0, "pv": 4200.0, "battery": -2500.0, "soc": 81.5, "load": 200.0}
    assert device.functions == {4}  # read-only registers are input registers


# ── SMA ─────────────────────────────────────────────────────────────────────

async def test_sma_hybrid_by_day(tmp_path: Path, modbus_device) -> None:
    await modbus_device(3, {
        **_s32(30773, 3000), **_s32(30961, 1500), **_s32(30967, S32_NAN),  # third input unused
        **_u32(31393, 2000), **_u32(31395, 0),  # charging 2 kW
        **_u32(30845, 64),
        **_s32(30865, 0), **_s32(30867, 1200),  # exporting 1.2 kW
    })
    src, values = _from_template("sma-hybrid", {"host": "127.0.0.1", "unit": 3})
    src["port"] = modbus_device.port
    got = await _bridge_values(tmp_path, src, values)
    assert got == {"grid": -1200.0, "pv": 4500.0, "battery": -2000.0, "soc": 64.0, "load": 1300.0}


async def test_sma_hybrid_at_night(tmp_path: Path, modbus_device) -> None:
    """A sleeping SMA inverter reports 'not available' for power: that's 0 W, not -2 GW."""
    await modbus_device(3, {
        **_s32(30773, S32_NAN), **_s32(30961, S32_NAN), **_s32(30967, S32_NAN),
        **_u32(31393, U32_NAN), **_u32(31395, 900),  # discharging 900 W
        **_u32(30845, 55),
        **_s32(30865, 250), **_s32(30867, 0),
    })
    src, values = _from_template("sma-hybrid", {"host": "127.0.0.1", "unit": 3})
    src["port"] = modbus_device.port
    got = await _bridge_values(tmp_path, src, values)
    assert got == {"grid": 250.0, "pv": 0.0, "battery": 900.0, "soc": 55.0, "load": 1150.0}


async def test_sma_battery_and_solar_inverters(modbus_device) -> None:
    await modbus_device(3, {**_s32(30775, -1800), **_u32(30845, U32_NAN)})
    src, _ = _from_template("sma-battery", {"host": "127.0.0.1", "unit": 3})
    source = create_source("sbs", src | {"port": modbus_device.port})
    await source.refresh()
    await source.stop()
    assert source.get("battery") == -1800.0  # AC power: charging
    assert source.get("soc") is None  # not available is missing, not 4294967295 %

    src, _ = _from_template("sma-inverter", {"host": "127.0.0.1", "unit": 3})
    source = create_source("sb", src | {"port": modbus_device.port, "unit": "3"})
    await source.refresh()
    await source.stop()
    assert source.get("pv") == -1800.0


def _meter_datagram(serial: int, readings: dict[int, int], protocol: int = 0x6069) -> bytes:
    """An SMA Energy Meter datagram; readings are measurement index → 0.1 W."""
    body = b""
    for index, value in readings.items():
        body += bytes([0, index, 4, 0]) + struct.pack(">I", value)
        body += bytes([0, index, 8, 0]) + struct.pack(">Q", 123456789)  # meter reading
    body += bytes([0x90, 0, 0, 0]) + bytes([2, 3, 4, 82])  # firmware 2.3.4.R
    body += bytes(4)  # end
    extra = b"\x00\x03" if protocol == 0x6081 else b""
    meter = struct.pack(">H", protocol) + extra + struct.pack(">HII", 372, serial, 1000) + body
    return b"SMA\0" + struct.pack(">HHI", 4, 0x02A0, 1) + struct.pack(">HH", len(meter), 0x10) + meter + bytes(4)


def test_sma_meter_datagrams() -> None:
    data = _meter_datagram(3012345678, {1: 0, 2: 25000, 21: 0, 22: 8000, 41: 0, 42: 9000, 61: 0, 62: 8000})
    assert parse_datagram(data) == (
        3012345678, {"grid": -2500.0, "grid_l1": -800.0, "grid_l2": -900.0, "grid_l3": -800.0})
    # Home Manager 2.0 unicast (firmware 2.07): two more header bytes, same readings
    assert parse_datagram(_meter_datagram(7, {1: 12345, 2: 0}, protocol=0x6081)) == (7, {"grid": 1234.5})
    # other Speedwire traffic: discovery, inverter protocol, truncated
    assert parse_datagram(bytes.fromhex("534d4100000402a000000001000200000001")) is None
    assert parse_datagram(_meter_datagram(7, {1: 1, 2: 0}, protocol=0x6065)) is None
    assert parse_datagram(data[:40]) == (3012345678, {})


async def test_sma_energy_meter_over_the_network(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    src, values = _from_template("sma-energy-meter", {"serial": ""})
    bridge = Bridge(parse({"name": "t", "sources": {"dev": src | {"port": port}}, "values": values}), tmp_path)
    source = bridge.sources["dev"]
    await source.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for _ in range(50):
                sender.sendto(_meter_datagram(3001, {1: 4321, 2: 0}), ("127.0.0.1", port))
                await asyncio.sleep(0.05)
                if source.get("grid") is not None:
                    break
        await bridge.update()
    finally:
        await source.stop()
    assert bridge.values["grid"] == 432.1
    assert source.status()["ok"]


def test_sma_meter_choice() -> None:
    source = SmaSpeedwireSource("em", {"type": "sma_speedwire"})
    source.handle(_meter_datagram(1111, {1: 1000, 2: 0}))
    source.handle(_meter_datagram(2222, {1: 5000, 2: 0}))
    assert source.get("grid") == 100.0  # the first meter heard
    status = source.status()
    assert not status["ok"] and "1111" in status["error"] and "2222" in status["error"]

    source = SmaSpeedwireSource("em", {"type": "sma_speedwire", "serial": "2222"})
    source.handle(_meter_datagram(1111, {1: 1000, 2: 0}))
    assert source.get("grid") is None and "1111" in source.status()["error"]
    source.handle(_meter_datagram(2222, {1: 5000, 2: 0}))
    assert source.get("grid") == 500.0 and source.status()["ok"]

    with pytest.raises(ValueError, match="interface"):
        SmaSpeedwireSource("em", {"type": "sma_speedwire", "interface": "eth0"})


# ── Selectronic ─────────────────────────────────────────────────────────────

async def test_selectronic(tmp_path: Path, free_port: int) -> None:
    async def point(request: web.Request) -> web.Response:
        return web.json_response({
            "device": {"name": "Selectronic SP-PRO"},
            "items": {
                "battery_soc": 88.5, "battery_w": 125.5,  # + discharging
                "grid_w": -3.5, "load_w": 1400.0, "shunt_w": 300.0, "solarinverter_w": 978.0,
                "timestamp": 1700000000,
            },
            "now": 1700000002,
        })

    app = web.Application()
    app.router.add_get("/cgi-bin/solarmonweb/devices/ABC123/point", point)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", free_port).start()
    try:  # host with a port, as for selpi
        src, values = _from_template("selectronic", {"host": f"127.0.0.1:{free_port}", "device": "ABC123"})
        got = await _bridge_values(tmp_path, src, values)
    finally:
        await runner.cleanup()
    assert got == {"grid": -3.5, "pv": 1278.0, "battery": 125.5, "soc": 88.5, "load": 1400.0}


# ── shared behaviour ────────────────────────────────────────────────────────

async def test_modbus_not_available_values(monkeypatch: pytest.MonkeyPatch) -> None:
    words = {1: struct.pack(">h", -0x8000), 2: struct.pack(">H", 0xFFFF), 3: struct.pack(">f", float("nan")),
             5: struct.pack(">i", -0x80000000), 7: struct.pack(">h", 12)}
    source = ModbusSource("m", {"type": "modbus", "host": "h", "fields": {
        "a": {"address": 1, "type": "int16"},
        "b": {"address": 2, "type": "uint16", "nan": 0},
        "c": {"address": 3, "type": "float32", "nan": -1, "scale": 1000},  # used as is, not scaled
        "d": {"address": 5, "type": "int32", "scale": 0.1},
        "e": {"address": 7, "type": "int16", "scale": 0.5, "nan": 0},
    }})

    async def fake_read(unit, address, count, input_registers=False):
        return words[address]

    monkeypatch.setattr(source._client, "read", fake_read)
    await source.refresh()
    got = {f: source.get(f) for f in "abcde"}
    assert got == {"a": None, "b": 0.0, "c": -1.0, "d": None, "e": 6.0}
    assert source.error is None  # "not available" is a normal answer

    with pytest.raises(ValueError, match="nan must be a number"):
        ModbusSource("m", {"type": "modbus", "host": "h", "fields": {"a": {"address": 1, "nan": "zero"}}})


def test_source_types_agree() -> None:
    from solar_bridge.app import config

    assert set(config.SOURCE_TYPES) == set(SOURCE_TYPES)


def test_settings_page_knows_fixed_readings() -> None:
    page = SETTINGS_PAGE.read_text()
    for name, cls in SOURCE_TYPES.items():
        if cls.READINGS:
            assert f"{name}: {json.dumps(list(cls.READINGS))}" in page, name
