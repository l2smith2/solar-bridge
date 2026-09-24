"""The standalone bridge: sources in, Fronius protocols out."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from ..core import EnergyCounters, ModbusTcpServer, RawMDNSAnnouncer, SolarApiServer, SunSpecMeter
from .config import Config, MeterConfig
from .sources import Source, create_source

_LOGGER = logging.getLogger(__name__)
SAVE_INTERVAL = 300  # s
STATIC = Path(__file__).parent / "static"


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
    def __init__(self, config: Config) -> None:
        self.config = config
        self.sources: dict[str, Source] = {n: create_source(n, c) for n, c in config.sources.items()}
        self.energy = EnergyCounters()
        self.meter_energy = {m.name: EnergyCounters() for m in config.meters}
        self.values: dict[str, float | None] = {}  # natural signs, for the web page
        self.data: dict[str, Any] = {}  # Fronius Solar API conventions
        self.meter_data: dict[str, dict[str, Any]] = {m.name: {} for m in config.meters}
        self.updated: datetime | None = None
        self.polls: dict[str, dict[str, Any]] = {}  # who polled us, for diagnostics
        self._energy_file = config.data_dir / "energy.json"
        self._next_save = 0.0
        self._http: SolarApiServer | None = None
        self._modbus: ModbusTcpServer | None = None
        self._mdns: RawMDNSAnnouncer | None = None

    # ── values ────────────────────────────────────────────────────────────

    def _value(self, name: str) -> float | None:
        ref = self.config.values.get(name)
        if ref is None:
            return None
        value = self.sources[ref.source].get(ref.field)
        if value is None:
            return None
        value *= ref.scale
        return -value if ref.invert else value

    def compute(self) -> None:
        cfg = self.config
        grid, pv, battery, soc, load = (self._value(n) for n in ("grid", "pv", "battery", "soc", "load"))
        if load is None and grid is not None:
            load = grid + (pv or 0.0) + (battery or 0.0)  # energy balance
        if soc is not None:
            soc = max(0.0, min(100.0, soc))
        self.values = {"grid": grid, "pv": pv, "battery": battery, "load": load, "soc": soc}
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
        if time.monotonic() >= self._next_save:
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
        data = {
            "main": self.energy.snapshot(),
            "meters": {n: c.snapshot() for n, c in self.meter_energy.items()},
        }
        self._energy_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._energy_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self._energy_file)  # atomic: a power cut never leaves half a file

    # ── web page ──────────────────────────────────────────────────────────

    def state(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "name": self.config.serial,
            "updated": self.updated.isoformat() if self.updated else None,
            "values": self.values,
            "energy": {"pv_today_wh": self.data.get("E_Day"), "grid_import_wh": self.data.get("_tot_wh_imp"),
                       "grid_export_wh": self.data.get("_tot_wh_exp")},
            "meters": [
                {"name": m.name, "unit_id": m.unit_id, "role": m.role,
                 "power": self.meter_data[m.name].get("P_Grid"),
                 "last_read_s": self._age(self._modbus.last_request.get(m.unit_id), now) if self._modbus else None}
                for m in self.config.meters
            ],
            "clients": {ip: {"path": p["path"], "age_s": self._age(p["at"], now)} for ip, p in self.polls.items()},
            "sources": {n: s.status() for n, s in self.sources.items()},
        }

    @staticmethod
    def _age(at: float | None, now: float) -> float | None:
        return None if at is None else round(now - at, 1)

    async def _handle_index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC / "index.html")

    async def _handle_state(self, request: web.Request) -> web.Response:
        return web.json_response(self.state())

    async def _track_poll(self, request: web.Request, response: web.StreamResponse) -> None:
        if request.path.startswith("/solar_api"):
            self.polls[request.remote or "?"] = {"path": request.path, "at": time.monotonic()}

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        cfg = self.config
        self.load_energy()
        for source in self.sources.values():
            await source.start()
        await self.update()

        self._http = SolarApiServer(lambda: self.data, cfg.http_port, cfg.serial, cfg.serial)
        self._http.app.router.add_get("/", self._handle_index)
        self._http.app.router.add_get("/api/state", self._handle_state)
        self._http.app.on_response_prepare.append(self._track_poll)
        await self._http.start()

        if cfg.meters:
            self._modbus = ModbusTcpServer(cfg.modbus_port)
            for meter in cfg.meters:
                self._modbus.meters[meter.unit_id] = SunSpecMeter(
                    lambda name=meter.name: self.meter_data[name], meter.name, meter.unit_id
                )
            await self._modbus.start()

        if cfg.wattpilot:
            self._mdns = RawMDNSAnnouncer(cfg.name, cfg.http_port, cfg.serial, cfg.serial)
            try:
                await self._mdns.async_start()
            except OSError as err:
                _LOGGER.error("mDNS announcements failed — the Wattpilot won't find us: %s", err)
                self._mdns = None
        _LOGGER.info("Solar Bridge '%s' running: web page on http://%s.local:%d/", cfg.serial, cfg.name, cfg.http_port)

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

    async def run(self, stop: asyncio.Event) -> None:
        await self.start()
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), self.config.update_interval)
                except TimeoutError:
                    await self.update()
        finally:
            await self.stop()
