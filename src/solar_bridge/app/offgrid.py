"""Off grid: show the Wattpilot a grid it can follow.

The Wattpilot's PV-surplus (Eco) mode charges from export to the grid. Off grid there is
none: spare solar either charges the battery or is held back — the battery inverter
raises the AC frequency (or talks to the solar inverters) to throttle them. So in place
of a grid meter the Wattpilot is shown a made-up one. A generator or other AC input
reads as import, so it never charges the car. The mode decides how the battery counts:

share — the car shares what would charge the battery:
- Battery charging reads as export, discharging as import (back off).
- The battery comes first: until it reaches car_start_soc, and again once it drops below
  car_stop_soc, the whole house reads as imported and the car waits.

assist — the battery charges as its inverter decides; the car only gets solar that is
being held back:
- Battery charging is left out, discharging reads as import. While solar isn't held back,
  a small import is shown so a charging car steps down rather than take the battery's share.

wattpilot — the Wattpilot is shown the real battery (power and charge), so its own
battery settings decide how much of it the car may use: Charges from, Discharges until
and Boost work as set in its app. The grid shown leaves the battery out (counting it here
too would count it twice, and battery discharge shown as import would stop Boost).

All modes: while solar is held back — the frequency is up, or without a frequency reading the
battery is full while solar is producing — offer_w more export is shown, so the Wattpilot
steps up and the battery inverter lets the solar inverters ramp up to cover the car. If
the battery starts discharging instead, there was less to spare than offered: no more
offers for a while, and the discharge (import) turns the car down.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import OffGridConfig

DISCHARGE_W = 150.0  # battery discharge that means solar isn't keeping up
MIN_PV_W = 200.0  # solar production that counts as daytime for the full-battery check
GENERATOR_W = 50.0  # AC input above this: a generator is running, offer nothing extra
OFFER_RESULT_S = 60.0  # a discharge this soon after an offer is that offer's result
BACKOFF_S = 300.0  # then no offers for this long
STOP_W = 1000.0  # import shown while the battery comes first, when house use is unknown
STEP_DOWN_W = 400.0  # assist: import shown while solar isn't held back


@dataclass
class OffGridStatus:
    grid: float | None  # what the Wattpilot is shown, W (+ import, - export)
    state: str  # no_data | battery_first | surplus | offering | backing_off
    reason: str


class OffGridController:
    def __init__(self, cfg: OffGridConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self._clock = clock
        self._car_allowed = False
        self._last_offer = float("-inf")
        self._backoff_until = float("-inf")

    def throttle_hz(self, frequency: float) -> float:
        if self.cfg.throttle_hz is not None:
            return self.cfg.throttle_hz
        return (60.0 if frequency > 55 else 50.0) + 0.2

    def update(
        self,
        battery: float | None,
        soc: float | None,
        generator: float | None = None,
        pv: float | None = None,
        load: float | None = None,
        frequency: float | None = None,
    ) -> OffGridStatus:
        """battery + discharging, generator + into the house, load + consuming (W); soc %."""
        cfg, now = self.cfg, self._clock()
        if battery is None or soc is None:
            return OffGridStatus(None, "no_data", "no battery reading — the Wattpilot pauses")
        generator = max(generator or 0.0, 0.0)
        assist = cfg.mode == "assist"

        if cfg.mode == "share":
            if soc >= cfg.car_start_soc:
                self._car_allowed = True
            elif soc < cfg.car_stop_soc:
                self._car_allowed = False
            if not self._car_allowed:
                house = load if load is not None and load > 0 else STOP_W
                return OffGridStatus(house, "battery_first",
                                     f"battery first: the car waits for {cfg.car_start_soc:g} %")

        # share: battery charging is surplus; assist: it's the battery's own; wattpilot: the Wattpilot's call
        grid = generator + {"share": battery, "assist": max(battery, 0.0)}.get(cfg.mode, 0.0)
        if battery > DISCHARGE_W and now - self._last_offer <= OFFER_RESULT_S:
            self._backoff_until = now + BACKOFF_S  # the offer was more than solar could give
            self._last_offer = float("-inf")
        held_back, why = self._held_back(soc, pv, frequency)
        if not held_back:
            if assist:
                return OffGridStatus(grid + STEP_DOWN_W, "battery_first",
                                     "solar isn't held back: the battery charges normally")
            if cfg.mode == "wattpilot":
                return OffGridStatus(grid, "surplus", "the Wattpilot's battery settings decide")
            return OffGridStatus(grid, "surplus", "battery charging is offered to the car")
        if generator > GENERATOR_W:
            return OffGridStatus(grid, "surplus", "generator running: no extra offer")
        if now < self._backoff_until:
            return OffGridStatus(grid, "backing_off",
                                 f"{why}, but the last offer drained the battery — waiting")
        if battery > DISCHARGE_W:
            return OffGridStatus(grid, "surplus", "battery discharging: no extra offer")
        self._last_offer = now
        return OffGridStatus(grid - cfg.offer_w, "offering", f"{why}: offering {cfg.offer_w:g} W more")

    def _held_back(self, soc: float, pv: float | None, frequency: float | None) -> tuple[bool, str]:
        if frequency is not None:
            limit = self.throttle_hz(frequency)
            return frequency >= limit, f"frequency {frequency:.2f} Hz ≥ {limit:.2f}"
        if soc >= self.cfg.full_soc and pv is not None and pv >= MIN_PV_W:
            return True, f"battery full ({soc:.0f} %) with solar producing"
        return False, ""
