"""Protocol emulation with no Home Assistant dependency (only aiohttp).

Shared by the standalone app (solar_bridge.app) and the Home Assistant
integration. Every server reads its values through a plain function returning
a dict in Fronius Solar API conventions — see solar_api.DataFn.
"""
from .energy import EnergyCounters
from .mdns import RawMDNSAnnouncer
from .meter import MeterReading, PhaseReading, read_meter
from .modbus import ModbusTcpServer, SunSpecMeter
from .solar_api import SolarApiServer

__all__ = [
    "EnergyCounters",
    "MeterReading",
    "ModbusTcpServer",
    "PhaseReading",
    "RawMDNSAnnouncer",
    "SolarApiServer",
    "SunSpecMeter",
    "read_meter",
]
