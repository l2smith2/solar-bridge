"""Read registers from any Modbus TCP device (SunSpec inverters and meters, Sungrow, ...).

    type: modbus
    host: 192.168.1.40
    port: 502          # optional
    unit: 1            # Modbus unit / slave ID
    fields:
      pv: {address: 40083, type: int16, scale_register: 40084}   # SunSpec W + W_SF
      grid: {address: 5600, type: int32, scale: -1, input: true}  # input register, negated

`address` is the wire address (register number - 1). Types: int16, uint16,
int32, uint32, float32. `scale` multiplies; `scale_register` is a SunSpec
scale factor (value * 10^sf). `swap_words: true` for little-endian word order.
"""
from __future__ import annotations

import asyncio
import struct
from typing import Any

from .base import Source

_TYPES = {"int16": (">h", 1), "uint16": (">H", 1), "int32": (">i", 2), "uint32": (">I", 2), "float32": (">f", 2)}


class ModbusClient:
    """Minimal async Modbus TCP client: read holding (FC3) / input (FC4) registers."""

    def __init__(self, host: str, port: int = 502, timeout: float = 5) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tid = 0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
        self._reader = self._writer = None

    async def read(self, unit: int, address: int, count: int, input_registers: bool = False) -> bytes:
        async with self._lock:
            try:
                return await asyncio.wait_for(self._read(unit, address, count, input_registers), self.timeout)
            except Exception:
                await self.close()  # reconnect next time
                raise

    async def _read(self, unit: int, address: int, count: int, input_registers: bool) -> bytes:
        if self._writer is None:
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        assert self._reader and self._writer
        self._tid = (self._tid + 1) & 0xFFFF
        func = 4 if input_registers else 3
        self._writer.write(struct.pack(">HHHBBHH", self._tid, 0, 6, unit, func, address, count))
        await self._writer.drain()
        tid, _, length, _ = struct.unpack(">HHHB", await self._reader.readexactly(7))
        pdu = await self._reader.readexactly(length - 1)
        if pdu[0] & 0x80:
            raise ConnectionError(f"Modbus exception {pdu[1]} reading {address}")
        if tid != self._tid or pdu[1] != count * 2:
            raise ConnectionError("unexpected Modbus response")
        return pdu[2:]


def decode(raw: bytes, kind: str, swap_words: bool = False) -> float:
    fmt, regs = _TYPES[kind]
    if swap_words and regs == 2:
        raw = raw[2:4] + raw[0:2]
    return float(struct.unpack(fmt, raw[: regs * 2])[0])


class ModbusSource(Source):
    def __init__(self, name: str, config: dict[str, Any]) -> None:
        super().__init__(name, config)
        if not config.get("host"):
            raise ValueError("needs a 'host'")
        self._fields: dict[str, dict[str, Any]] = config.get("fields") or {}
        if not self._fields:
            raise ValueError("needs 'fields'")
        for fname, spec in self._fields.items():
            if not isinstance(spec, dict) or "address" not in spec:
                raise ValueError(f"field {fname} needs an 'address'")
            if spec.get("type", "int16") not in _TYPES:
                raise ValueError(f"field {fname}: type must be one of {', '.join(_TYPES)}")
        self._client = ModbusClient(config["host"], int(config.get("port", 502)), float(config.get("timeout", 5)))
        self._unit = int(config.get("unit", 1))

    async def stop(self) -> None:
        await self._client.close()

    async def poll(self) -> None:
        for fname, spec in self._fields.items():
            kind = spec.get("type", "int16")
            is_input = bool(spec.get("input", False))
            raw = await self._client.read(self._unit, int(spec["address"]), _TYPES[kind][1], is_input)
            value = decode(raw, kind, bool(spec.get("swap_words", False))) * float(spec.get("scale", 1))
            if "scale_register" in spec:
                sf_raw = await self._client.read(self._unit, int(spec["scale_register"]), 1, is_input)
                value *= 10 ** decode(sf_raw, "int16")
            self.set(fname, value)
