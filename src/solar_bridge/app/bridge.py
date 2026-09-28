"""The bridge runtime: sources in, Fronius protocols out.

One Bridge runs one configuration. Saving new settings replaces it (see web.py).
With no configuration it runs in setup mode: just the web page.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from ..core import EnergyCounters, ModbusTcpServer, RawMDNSAnnouncer, SolarApiServer, SunSpecMeter
from .config import Config, MeterConfig
from .offgrid import OffGridController, OffGridStatus
from .sources import Source, create_source

_LOGGER = logging.getLogger(__name__)
SAVE_INTERVAL = 300  # s


def meter_power(meter: MeterConfig, value: float | None) -> float | None:
    """Power on the meter's wire: + = from the grid side into the device.

    Values are "natural" (battery + discharging, pv + producing, load + consuming),
    so a generator-position meter negates: producing reads as flowing to the grid.
    """
    if value is None:
        return None
    power = -value if meter.role == "generator" else value
    return -power if meter.invert else power


class Bridge:
    def __init__(
        self,
        config: Config | None,
        data_dir: Path,
        http_port: int = 80,
        add_routes: Callable[[web.Application], None] | None = None,
    ) -> None:
        self.config = config
        self.data_dir = (config.data_dir if config and config.data_dir else data_dir)
        self.http_port = config.http_port if config else http_port
        self._add_routes = add_routes
        self.sources: dict[str, Source] = {}
        self.problems: list[str] = []  # non-fatal start-up problems, shown on the web page
        for name, scfg in (config.sources if config else {}).items():
            try:
                self.sources[name] = create_source(name, scfg)
            except ValueError as err:
                self.problems.append(str(err))
        meters = config.meters if config else []
        self.energy = EnergyCounters()
        self.meter_energy = {m.name: EnergyCounters() for m in meters}
        self.values: dict[str, float | None] = {}  # natural signs, for the web page
        self.data: dict[str, Any] = {}  # Fronius Solar API conventions
        self.meter_data: dict[str, dict[str, Any]] = {m.name: {} for m in meters}
        self.off_grid = OffGridController(config.off_grid) if config and config.off_grid.enabled else None
        self.off_grid_status: OffGridStatus | None = None
        self.updated: datetime | None = None
        self.polls: dict[str, dict[str, Any]] = {}  # who polled us, for diagnostics
        self._energy_file = self.data_dir / "energy.json"
        self._next_save = 0.0
        self._http: SolarApiServer | None = None
        self._modbus: ModbusTcpServer | None = None
        self._mdns: RawMDNSAnnouncer | None = None

    @property
    def interval(self) -> float:
        return self.config.update_interval if self.config else 5.0

    # ── values ────────────────────────────────────────────────────────────

    def _value(self, name: str) -> float | None:
        """A configured value; several parts are added up (None only if none has a reading)."""
        ref = self.config.values.get(name) if self.config else None
        if ref is None:
            return None
        readings = [
            self.sources[source].get(field) if source in self.sources else None for source, field in ref.parts
        ]
        present = [r for r in readings if r is not None]
        if not present:
            return None
        value = sum(present) * ref.scale
        return -value if ref.invert else value

    def compute(self) -> None:
        if self.config is None:
            return
        cfg = self.config
        grid, pv, battery, soc, load, frequency = (
            self._value(n) for n in ("grid", "pv", "battery", "soc", "load", "frequency"))
        if load is None and (grid is not None or (self.off_grid and pv is not None)):
            load = (grid or 0.0) + (pv or 0.0) + (battery or 0.0)  # energy balance (off grid: grid = generator)
        if soc is not None:
            soc = max(0.0, min(100.0, soc))
        self.values = {"grid": grid, "pv": pv, "battery": battery, "load": load, "soc": soc, "frequency": frequency}
        self.data = {
            "P_Grid": grid,
            "P_PV": pv,
            "P_Akku": battery,
            "P_Load": -load if load is not None else None,  # Fronius: consumption negative
            "SOC": soc,
            "grid_phases": cfg.grid_phases,
            "grid_ct_rating": cfg.breaker_amps,
            **self.energy.update(pv, grid),
        }
        if self.off_grid:
            status = self.off_grid_status = self.off_grid.update(battery, soc, grid, pv, load, frequency)
            # The Wattpilot sees a grid made up from the battery (offgrid.py). The battery itself is
            # hidden from it, so its own battery rules don't count the same power twice.
            self.data.update(
                P_Grid=status.grid,
                P_Akku=None,
                SOC=None,
                P_Load=None if status.grid is None or pv is None else -(status.grid + pv),
            )
        for meter in cfg.meters:
            power = meter_power(meter, self.values.get(meter.value))
            self.meter_data[meter.name] = {
                "P_Grid": power,
                "grid_phases": cfg.grid_phases,
                **self.meter_energy[meter.name].update(None, power),
            }
        self.updated = datetime.now().astimezone()

    async def update(self) -> None:
        await asyncio.gather(*(s.refresh() for s in self.sources.values()))
        self.compute()
        if self.config and time.monotonic() >= self._next_save:
            self._next_save = time.monotonic() + SAVE_INTERVAL
            await asyncio.get_running_loop().run_in_executor(None, self.save_energy)

    # ── energy persistence ────────────────────────────────────────────────

    def load_energy(self) -> None:
        try:
            stored = json.loads(self._energy_file.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            _LOGGER.warning("Ignoring unreadable %s: %s", self._energy_file, err)
            return
        self.energy.restore(stored.get("main"))
        for name, counters in self.meter_energy.items():
            counters.restore(stored.get("meters", {}).get(name))

    def save_energy(self) -> None:
        if self.config is None:
            return
        data = {
            "main": self.energy.snapshot(),
            "meters": {n: c.snapshot() for n, c in self.meter_energy.items()},
        }
        self._energy_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._energy_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self._energy_file)  # atomic: a power cut never leaves half a file

    # ── status for the web page ───────────────────────────────────────────

    def state(self) -> dict[str, Any]:
        now = time.monotonic()
        cfg = self.config
        return {
            "configured": cfg is not None,
            "name": cfg.serial if cfg else "Solar Bridge",
            "updated": self.updated.isoformat() if self.updated else None,
            "values": self.values,
            "energy": {"pv_today_wh": self.data.get("E_Day"), "grid_import_wh": self.data.get("_tot_wh_imp"),
                       "grid_export_wh": self.data.get("_tot_wh_exp")},
            "meters": [
                {"name": m.name, "unit_id": m.unit_id, "role": m.role,
                 "power": self.meter_data[m.name].get("P_Grid"),
                 "last_read_s": self._age(self._modbus.last_request.get(m.unit_id), now) if self._modbus else None}
                for m in (cfg.meters if cfg else [])
            ],
            "wattpilot": bool(cfg and cfg.wattpilot),
            "off_grid": None if self.off_grid_status is None else {
                "wattpilot_grid": self.off_grid_status.grid,
                "state": self.off_grid_status.state,
                "reason": self.off_grid_status.reason,
            },
            "clients": {ip: {"path": p["path"], "age_s": self._age(p["at"], now)} for ip, p in self.polls.items()},
            "sources": {n: s.status() for n, s in self.sources.items()},
            "problems": self.problems,
        }

    @staticmethod
    def _age(at: float | None, now: float) -> float | None:
        return None if at is None else round(now - at, 1)

    async def _track_poll(self, request: web.Request, response: web.StreamResponse) -> None:
        if request.path.startswith("/solar_api"):
            self.polls[request.remote or "?"] = {"path": request.path, "at": time.monotonic()}

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start serving. Raises OSError only if the web/Solar API port can't be opened."""
        cfg = self.config
        self.load_energy()
        for source in self.sources.values():
            await source.start()
        await self.update()

        serial = cfg.serial if cfg else "SolarBridge"
        self._http = SolarApiServer(lambda: self.data, self.http_port, serial, serial)
        if self._add_routes:
            self._add_routes(self._http.app)
        self._http.app.on_response_prepare.append(self._track_poll)
        await self._http.start()
        if cfg is None:
            _LOGGER.info("Not configured yet — open http://<this device>:%d/settings", self.http_port)
            return

        if cfg.meters:
            modbus = ModbusTcpServer(cfg.modbus_port)
            for meter in cfg.meters:
                modbus.meters[meter.unit_id] = SunSpecMeter(
                    lambda name=meter.name: self.meter_data[name], meter.name, meter.unit_id
                )
            try:
                await modbus.start()
                self._modbus = modbus
            except OSError as err:
                self.problems.append(f"Smart Meter IP (Modbus) port {cfg.modbus_port} unavailable: {err}")
                _LOGGER.error(self.problems[-1])

        if cfg.wattpilot:
            mdns = RawMDNSAnnouncer(cfg.name, self.http_port, cfg.serial, cfg.serial)
            try:
                await mdns.async_start()
                self._mdns = mdns
            except OSError as err:
                self.problems.append(f"Network announcement (mDNS) failed — the Wattpilot won't find this: {err}")
                _LOGGER.error(self.problems[-1])
        _LOGGER.info("Solar Bridge '%s' running on port %d", cfg.serial, self.http_port)

    async def stop(self) -> None:
        for stop in (
            self._mdns.async_stop if self._mdns else None,
            self._modbus.stop if self._modbus else None,
            self._http.stop if self._http else None,
            *(s.stop for s in self.sources.values()),
        ):
            if stop:
                try:
                    await stop()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Error while stopping")
        self.save_energy()
