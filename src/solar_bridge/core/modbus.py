"""
Fronius Smart Meter IP emulation via SunSpec Modbus TCP.

One TCP server per port serves any number of virtual meters, routed by Modbus
unit ID — e.g. the grid meter at unit 240 and an AC-coupled battery meter at
unit 241, both on port 502. Requests for unknown unit IDs are not answered.
Add meters to ModbusTcpServer.meters (before or after start).

Register layout per meter (SunSpec model 213, float). Numbers are Modbus
register numbers (1-based); wire address = register - 1.

  40001-40002  SunS identifier (0x53756e53)
  40003        Common model ID = 1
  40004        Common model length = 65
  40005-40020  Manufacturer: "Fronius"
  40021-40036  Model: "Smart Meter IP"
  40037-40044  Options: "1.0"
  40045-40052  SW Version: "1.0.0"
  40053-40068  Serial number
  40069        Modbus device address (= unit ID)
  40070        Model ID = 213 (three-phase WYE)
  40071        Length = 124
  40072-40079  A, AphA, AphB, AphC                  float32
  40080-40087  PhV, PhVphA, PhVphB, PhVphC          float32
  40088-40095  PPV, PPVphAB, PPVphBC, PPVphCA       float32
  40096-40097  Hz                                   float32
  40098-40105  W, WphA, WphB, WphC  ← total real power at wire 40097 (confirmed live)
  40106-40113  VA, VAphA, VAphB, VAphC              float32
  40114-40121  VAR, VARphA, VARphB, VARphC          float32
  40122-40129  PF, PFphA, PFphB, PFphC              float32
  40130-40137  TotWhExp, TotWhExpPhA/B/C            float32
  40138-40145  TotWhImp, TotWhImpPhA/B/C            float32
  40146-40161  TotVAhExp/Imp (+ per phase)          float32, zero
  40162-40193  TotVArh quadrants (+ per phase)      float32, zero
  40194-40195  Evt                                  uint32 = 0
  40196        End model ID = 0xFFFF
  40197        End model length = 0

Sign convention: W positive = power flowing from the grid side into whatever
the meter measures (grid import, load consumption, battery charging).
"""
from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from collections.abc import Callable, Mapping
from typing import Any

from .meter import read_meter

# Returns the current values (P_Grid, grid_phases, _tot_wh_imp/_exp, per-phase keys)
DataFn = Callable[[], Mapping[str, Any] | None]

_LOGGER = logging.getLogger(__name__)

SUNSPEC_SID = 0x53756E53  # 'SunS'
SUNSPEC_END = 0xFFFF
BLOCK_START = 40000  # wire address of the first register (40001)

FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
EXC_ILLEGAL_FUNCTION = 0x01
EXC_ILLEGAL_DATA_VALUE = 0x03
EXC_DEVICE_FAILURE = 0x04
MAX_READ_COUNT = 125  # Modbus limit for FC 3/4

_STOP_TIMEOUT = 5  # s


def _string(text: str, num_regs: int) -> bytes:
    return text.encode("ascii", "replace")[: num_regs * 2].ljust(num_regs * 2, b"\x00")


class SunSpecMeter:
    """One virtual Smart Meter IP, reading live values from a data function."""

    def __init__(self, data: DataFn, serial: str, unit_id: int) -> None:
        self._data = data
        self.serial = serial
        self.unit_id = unit_id

    def register_block(self) -> bytes:
        """Registers 40001-40197 as raw bytes (2 bytes per register)."""
        m = read_meter(self._data() or {})
        # Always three phases on the wire; unused phases carry nominal voltage, no load
        phases = list(m.phases) + [None] * (3 - len(m.phases))
        nominal = m.phases[0].v
        volts = [ph.v if ph else nominal for ph in phases]
        amps = [ph.i if ph else 0.0 for ph in phases]
        watts = [ph.p if ph else 0.0 for ph in phases]
        va = [abs(ph.s) if ph else 0.0 for ph in phases]
        var = [ph.q if ph else 0.0 for ph in phases]
        pf = [ph.pf if ph else 1.0 for ph in phases]
        share = [1 / len(m.phases) if ph else 0.0 for ph in phases]
        ll = [(volts[a] + volts[b]) / 2 * math.sqrt(3) for a, b in ((0, 1), (1, 2), (2, 0))]

        floats = [
            m.i, *amps,
            sum(v for v, ph in zip(volts, phases, strict=True) if ph) / len(m.phases), *volts,
            sum(ll) / 3, *ll,
            50.0,
            m.p, *watts,
            sum(va), *va,
            m.q, *var,
            m.pf, *pf,
            m.wh_exp, *(m.wh_exp * k for k in share),
            m.wh_imp, *(m.wh_imp * k for k in share),
            *([0.0] * 24),  # VAh and VArh counters
        ]
        return (
            struct.pack(">IHH", SUNSPEC_SID, 1, 65)
            + _string("Fronius", 16)
            + _string("Smart Meter IP", 16)
            + _string("1.0", 8)
            + _string("1.0.0", 8)
            + _string(self.serial, 16)
            + struct.pack(">HHH", self.unit_id, 213, 124)
            + struct.pack(f">{len(floats)}f", *floats)
            + struct.pack(">IHH", 0, SUNSPEC_END, 0)
        )

    def read(self, address: int, count: int) -> bytes:
        """Registers [address, address + count); unmapped registers read as 0."""
        block = self.register_block()
        start = (address - BLOCK_START) * 2
        end = start + count * 2
        head = b"\x00\x00" * max(0, min(-start, count * 2) // 2)
        body = block[max(start, 0) : max(end, 0)]
        return (head + body).ljust(count * 2, b"\x00")


class ModbusTcpServer:
    """Minimal async Modbus TCP server (FC 3/4) for SunSpec meters."""

    def __init__(self, port: int, host: str = "0.0.0.0") -> None:
        self.port = port
        self.host = host
        self.meters: dict[int, SunSpecMeter] = {}
        self.last_request: dict[int, float] = {}  # unit ID -> time.monotonic() of last read
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        _LOGGER.info("Modbus TCP server started on port %d", self.port)

    async def stop(self) -> None:
        """Stop listening and drop clients.

        Server.wait_closed() waits for every open connection (Python 3.12+), and
        Fronius keeps its connection open — so clients must be closed first or
        unloading the entry would hang forever.
        """
        if self._server is None:
            return
        self._server.close()
        for writer in list(self._clients):
            writer.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), _STOP_TIMEOUT)
        except TimeoutError:
            _LOGGER.warning("Modbus server on port %d did not close cleanly", self.port)
        self._server = None
        _LOGGER.info("Modbus TCP server on port %d stopped", self.port)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        _LOGGER.debug("Modbus client connected: %s", peer)
        self._clients.add(writer)
        try:
            while True:
                # MBAP header (7 bytes incl. unit ID) + PDU
                tid, _proto, length, unit_id = struct.unpack(">HHHB", await reader.readexactly(7))
                if not 2 <= length <= 254:
                    break
                pdu = await reader.readexactly(length - 1)
                meter = self.meters.get(unit_id)
                if meter is None:
                    _LOGGER.debug("No meter at unit %d on port %d", unit_id, self.port)
                    continue
                self.last_request[unit_id] = time.monotonic()
                reply = self._reply(meter, pdu)
                writer.write(struct.pack(">HHHB", tid, 0, len(reply) + 1, unit_id) + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()
            _LOGGER.debug("Modbus client disconnected: %s", peer)

    @staticmethod
    def _reply(meter: SunSpecMeter, pdu: bytes) -> bytes:
        """Response PDU (without unit ID) for a request PDU."""
        func = pdu[0]
        if func not in (FC_READ_HOLDING, FC_READ_INPUT):
            return bytes([func | 0x80, EXC_ILLEGAL_FUNCTION])
        if len(pdu) < 5:
            return bytes([func | 0x80, EXC_ILLEGAL_DATA_VALUE])
        address, count = struct.unpack(">HH", pdu[1:5])
        if not 1 <= count <= MAX_READ_COUNT:
            return bytes([func | 0x80, EXC_ILLEGAL_DATA_VALUE])
        try:
            data = meter.read(address, count)
        except Exception:
            _LOGGER.exception("Error building Modbus registers for unit %d", meter.unit_id)
            return bytes([func | 0x80, EXC_DEVICE_FAILURE])
        return bytes([func, len(data)]) + data
