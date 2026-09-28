"""Tesla Powerwall / Tesla Energy Gateway via pypowerwall (local, TEDAPI or cloud).

Readings: grid (+ importing), solar, battery (+ discharging), load (+ consuming),
soc (% as shown in the Tesla app).
"""
from __future__ import annotations

import asyncio
from typing import Any

from .base import Source

_PASSTHROUGH = ("host", "password", "email", "timezone", "gw_pwd", "cloudmode", "fleetapi", "siteid", "authpath")


class TeslaSource(Source):
    def __init__(self, name: str, config: dict[str, Any]) -> None:
        super().__init__(name, config)
        self._pw: Any = None

    def _connect(self) -> Any:
        try:
            import pypowerwall  # add-on: settings page → Install, or pip install 'solar-bridge[tesla]'
        except ImportError as err:
            raise RuntimeError("Tesla support isn't installed — install it from the settings page") from err

        kwargs = {k: self.config[k] for k in _PASSTHROUGH if k in self.config}
        kwargs.update(self.config.get("options") or {})
        return pypowerwall.Powerwall(**kwargs)

    def _read(self) -> dict[str, float | None]:
        if self._pw is None:
            self._pw = self._connect()
        pw = self._pw
        return {
            "grid": pw.grid(),
            "solar": pw.solar(),
            "battery": pw.battery(),
            "load": pw.home(),
            "soc": pw.level(scale=bool(self.config.get("scale_soc", True))),
        }

    async def poll(self) -> None:
        readings = await asyncio.get_running_loop().run_in_executor(None, self._read)
        if all(v is None for v in readings.values()):
            self._pw = None  # reconnect next time
            raise ConnectionError("no data from the Tesla gateway")
        for field, value in readings.items():
            if value is not None:
                self.set(field, value)
