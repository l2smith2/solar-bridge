"""Modbus TCP Smart Meter IP server."""
from __future__ import annotations

import asyncio
import struct
import time

import pytest

from solar_bridge.core.modbus import BLOCK_START, ModbusTcpServer, SunSpecMeter


def _meter(data: dict, unit_id: int = 240, serial: str = "MyHome") -> SunSpecMeter:
    return SunSpecMeter(lambda: data, serial, unit_id)


def _float(block: bytes, wire_address: int) -> float:
    offset = (wire_address - BLOCK_START) * 2
    return struct.unpack(">f", block[offset : offset + 4])[0]


def _u16(block: bytes, wire_address: int) -> int:
    offset = (wire_address - BLOCK_START) * 2
    return struct.unpack(">H", block[offset : offset + 2])[0]


def test_register_layout() -> None:
    """Key registers sit where the SnapIN was confirmed to read them."""
    block = _meter({"P_Grid": -1500.0, "grid_phases": 1, "_tot_wh_imp": 10.0, "_tot_wh_exp": 20.0}).register_block()
    assert len(block) == 197 * 2  # registers 40001-40197
    assert block[:4] == b"SunS"
    assert _u16(block, 40002) == 1 and _u16(block, 40003) == 65  # common model
    assert block[(40004 - BLOCK_START) * 2 :].startswith(b"Fronius\x00")
    assert block[(40052 - BLOCK_START) * 2 :].startswith(b"MyHome\x00")
    assert _u16(block, 40068) == 240  # DA
    assert _u16(block, 40069) == 213 and _u16(block, 40070) == 124
    assert _float(block, 40095) == 50.0  # Hz
    assert _float(block, 40097) == -1500.0  # W
    assert _float(block, 40099) == -1500.0  # WphA — single phase: all on A
    assert _float(block, 40101) == 0.0
    assert _float(block, 40129) == 20.0  # TotWhExp
    assert _float(block, 40137) == 10.0  # TotWhImp
    assert _u16(block, 40195) == 0xFFFF and _u16(block, 40196) == 0


def test_three_phase_split_and_non_ascii_serial() -> None:
    block = _meter({"P_Grid": 3000.0, "grid_phases": 3}, serial="Zuhause Süd").register_block()
    assert [_float(block, a) for a in (40099, 40101, 40103)] == [1000.0, 1000.0, 1000.0]
    assert block[(40052 - BLOCK_START) * 2 :].startswith(b"Zuhause S?d")


def test_read_window() -> None:
    meter = _meter({"P_Grid": 1.0})
    block = meter.register_block()
    assert meter.read(40097, 2) == block[194:198]
    assert meter.read(39998, 4) == b"\x00" * 4 + block[:4]  # before the block reads 0
    assert meter.read(40195, 4) == block[-4:] + b"\x00" * 4  # past the end reads 0


async def _request(reader, writer, unit: int, address: int, count: int, func: int = 3) -> bytes:
    writer.write(struct.pack(">HHHBBHH", 7, 0, 6, unit, func, address, count))
    await writer.drain()
    header = await asyncio.wait_for(reader.readexactly(7), 2)
    length = struct.unpack(">H", header[4:6])[0]
    return await reader.readexactly(length - 1)


async def test_shared_port_routes_by_unit_id(free_port: int) -> None:
    """Also records when each unit was last read (for the status page)."""
    server = ModbusTcpServer(free_port)
    server.meters[240] = _meter({"P_Grid": 1200.0}, 240)
    server.meters[241] = _meter({"P_Grid": -800.0}, 241, "AC-Battery")
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
        pdu = await _request(reader, writer, 240, 40097, 2)
        assert pdu[:2] == b"\x03\x04" and struct.unpack(">f", pdu[2:])[0] == 1200.0
        pdu = await _request(reader, writer, 241, 40097, 2, func=4)
        assert struct.unpack(">f", pdu[2:])[0] == -800.0
        pdu = await _request(reader, writer, 241, 40068, 1)
        assert pdu == b"\x03\x02\x00\xf1"  # DA = 241
        assert set(server.last_request) == {240, 241}

        assert await _request(reader, writer, 240, 40000, 126) == b"\x83\x03"  # > 125 registers
        assert await _request(reader, writer, 240, 40000, 1, func=6) == b"\x86\x01"

        # Unknown unit: no answer (like a gateway without that device)
        writer.write(struct.pack(">HHHBBHH", 9, 0, 6, 1, 3, 40000, 2))
        await writer.drain()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reader.readexactly(1), 0.3)
    finally:
        await server.stop()


async def test_stop_with_connected_client(free_port: int) -> None:
    """Regression: stop() hung forever while Fronius kept its connection open,
    so reloading the entry never finished and every sensor went unavailable."""
    server = ModbusTcpServer(free_port)
    server.meters[240] = _meter({"P_Grid": 5.0})
    await server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
    await _request(reader, writer, 240, 40097, 2)

    started = time.monotonic()
    await asyncio.wait_for(server.stop(), 3)
    assert time.monotonic() - started < 1
    assert await reader.read() == b""  # client was disconnected
    writer.close()
