"""SMA Energy Meter / Sunny Home Manager 2.0: the readings they send on the network.

    type: sma_speedwire
    serial: 3012345678        # optional: which meter, when there are several
    interface: 192.168.1.20   # optional: this device's IP on the meter's network

Readings: grid (+ importing) and grid_l1/l2/l3 per phase, in W, and frequency in Hz
(Energy Meter 2.0 firmware 2.03.4 and later, Home Manager 2.0). The meters send
them every second to 239.12.255.254:9522 (SMA Speedwire), so there is nothing
to set up on the meter; it only has to be on the same network.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from typing import Any

from .base import Source, number

_LOGGER = logging.getLogger(__name__)

GROUP, PORT = "239.12.255.254", 9522
# SMA Net 2 protocol IDs of meter readings → where the meter part starts. 0x6081 is what
# Home Manager 2.0 firmware 2.07 sends by unicast: 2 more header bytes, same readings.
_METER_DATA = {0x6069: 18, 0x6081: 20}
# reading → (import, export) measurement indexes; values in 0.1 W
_POWER = {"grid": (1, 2), "grid_l1": (21, 22), "grid_l2": (41, 42), "grid_l3": (61, 62)}
_FREQUENCY = 14  # measurement index; value in 0.001 Hz


def parse_datagram(data: bytes) -> tuple[int, dict[str, float]] | None:
    """(serial, readings) from an Energy Meter datagram; None for anything else.

    Layout: "SMA\\0", a group tag, data length at 12, protocol ID at 16; then the
    meter part: SUSy ID, serial, ticker, and OBIS entries of channel, index, type
    and tariff bytes, followed by 4 bytes (type 4, present value) or 8 (type 8,
    meter reading). Channel 0x90 carries the 4-byte firmware version; 0 0 0 0 ends it.
    """
    if len(data) < 30 or data[:4] != b"SMA\0":
        return None
    start = _METER_DATA.get(struct.unpack_from(">H", data, 16)[0])
    if start is None:
        return None
    end = min(len(data), 16 + struct.unpack_from(">H", data, 12)[0])
    serial = struct.unpack_from(">I", data, start + 2)[0]
    present: dict[int, int] = {}
    pos = start + 10
    while pos + 8 <= end:
        channel, index, kind = data[pos], data[pos + 1], data[pos + 2]
        if kind == 4:
            if channel == 0:
                present[index] = struct.unpack_from(">I", data, pos + 4)[0]
            pos += 8
        elif kind == 8:
            pos += 12
        elif channel == 0x90 and kind == 0:  # firmware version
            pos += 8
        else:  # end marker, or something this parser doesn't know the length of
            break
    readings = {
        name: (present[imp] - present[exp]) / 10
        for name, (imp, exp) in _POWER.items()
        if imp in present and exp in present
    }
    if _FREQUENCY in present:
        readings["frequency"] = present[_FREQUENCY] / 1000
    return serial, readings


def open_socket(interface: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):  # other listeners (e.g. SMA software) may share the port
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", port))
        membership = socket.inet_aton(GROUP) + socket.inet_aton(interface)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, source: SmaSpeedwireSource) -> None:
        self.source = source

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.source.handle(data)


class SmaSpeedwireSource(Source):
    READINGS = (*_POWER, "frequency")
    push = True
    silence_hint = (
        "nothing heard from an SMA Energy Meter or Sunny Home Manager within 10 s. "
        "It must be on the same network as this device"
    )

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        super().__init__(name, config)
        self.fields = {reading: {} for reading in self.READINGS}
        self._serial = None if config.get("serial") in (None, "") else number(config, "serial", 0)
        self._interface = str(config.get("interface") or "0.0.0.0")
        try:
            socket.inet_aton(self._interface)
        except OSError:
            raise ValueError("interface must be this device's IP address, e.g. 192.168.1.20") from None
        self._port = number(config, "port", PORT)
        self._task: asyncio.Task | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._heard: dict[int, float] = {}  # serial → when last heard
        self._using: int | None = self._serial

    async def start(self) -> None:
        self._task = asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        loop = asyncio.get_running_loop()
        while True:  # at boot the network may not be up yet
            try:
                sock = open_socket(self._interface, self._port)
                self._transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=sock)
                self.error = None
                return
            except OSError as err:
                if self.error is None:
                    _LOGGER.warning("Source %s: can't listen for SMA meters: %s — retrying", self.name, err)
                self.error = f"can't listen for SMA meters: {err}"
                await asyncio.sleep(10)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._transport:
            self._transport.close()
            self._transport = None

    def handle(self, data: bytes) -> None:
        parsed = parse_datagram(data)
        if parsed is None:
            return
        serial, readings = parsed
        self._heard[serial] = time.monotonic()
        if self._using is None:
            self._using = serial  # no serial set: the first meter heard
        if serial == self._using:
            for reading, value in readings.items():
                self.set(reading, value)

    def status(self) -> dict[str, Any]:
        status = super().status()
        now = time.monotonic()
        heard = sorted(s for s, t in self._heard.items() if now - t <= self.stale_after)
        others = ", ".join(str(s) for s in heard if s != self._using)
        if self._serial is not None and self._serial not in heard and heard:
            status.update(ok=False, error=f"meter {self._serial} not heard; heard {others}")
        elif self._serial is None and others:
            status.update(ok=False, error=(
                f"several SMA meters heard: using {self._using}, also {others}. "
                "Enter the grid meter's serial number"))
        return status
