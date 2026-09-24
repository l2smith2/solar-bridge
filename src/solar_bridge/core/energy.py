"""Energy counters integrated from power readings.

Persistence is the caller's job: save snapshot() somewhere and pass it back to
restore() on start, so meter totals never go backwards across restarts (Fronius
and Solar.web log energy from them).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from time import monotonic
from typing import Any

MAX_GAP = 300  # s — cap on one integration step (e.g. after a stall)
KEYS = ("E_Day", "E_Year", "E_Total", "_tot_wh_imp", "_tot_wh_exp")


class EnergyCounters:
    """PV energy (E_Day/E_Year/E_Total) and meter import/export totals, in Wh."""

    def __init__(
        self,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._now = now
        self._clock = clock
        self.values: dict[str, float] = dict.fromkeys(KEYS, 0.0)
        self._day: str | None = None
        self._year: int | None = None
        self._last: float | None = None

    def restore(self, stored: Mapping[str, Any] | None) -> None:
        stored = stored or {}
        for key in KEYS:
            if isinstance(stored.get(key), (int, float)):
                self.values[key] = float(stored[key])
        self._day = stored.get("day")
        self._year = stored.get("year")

    def snapshot(self) -> dict[str, Any]:
        return {**self.values, "day": self._day, "year": self._year}

    def update(self, p_pv: float | None, p_meter: float | None) -> dict[str, float]:
        """Add energy since the last call. p_meter: + = import (into the device)."""
        now = self._now()
        if self._day != now.date().isoformat():
            self._day = now.date().isoformat()
            self.values["E_Day"] = 0.0
        if self._year != now.year:
            self._year = now.year
            self.values["E_Year"] = 0.0

        tick = self._clock()
        hours = 0.0 if self._last is None else min(tick - self._last, MAX_GAP) / 3600
        self._last = tick

        if p_pv is not None and p_pv > 0:
            for key in ("E_Day", "E_Year", "E_Total"):
                self.values[key] += p_pv * hours
        if p_meter is not None:
            key = "_tot_wh_imp" if p_meter > 0 else "_tot_wh_exp"
            self.values[key] += abs(p_meter) * hours
        return dict(self.values)
