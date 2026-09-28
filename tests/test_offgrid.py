"""Off grid: the grid the Wattpilot is shown, from battery, generator, solar and frequency."""
from __future__ import annotations

from pathlib import Path

import pytest

from solar_bridge.app.bridge import Bridge
from solar_bridge.app.config import ConfigError, OffGridConfig, parse
from solar_bridge.app.offgrid import BACKOFF_S, STOP_W, OffGridController


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _controller(**settings) -> tuple[OffGridController, Clock]:
    clock = Clock()
    return OffGridController(OffGridConfig(enabled=True, **settings), clock), clock


def test_battery_first_with_hysteresis() -> None:
    og, _ = _controller(car_start_soc=90, car_stop_soc=80)
    st = og.update(battery=-3000, soc=70, load=800)
    assert (st.grid, st.state) == (800, "battery_first")  # the house reads as imported: the car waits
    assert og.update(battery=-3000, soc=70).grid == STOP_W  # house use unknown
    assert og.update(battery=-3000, soc=85).state == "battery_first"  # not yet at the start charge
    assert og.update(battery=-3000, soc=90).grid == -3000  # battery charging = surplus for the car
    assert og.update(battery=-3000, soc=85).grid == -3000  # keeps going down to the stop charge
    assert og.update(battery=-3000, soc=79.5).state == "battery_first"
    assert og.update(battery=-3000, soc=85).state == "battery_first"


def test_surplus_follows_the_battery_and_never_the_generator() -> None:
    og, _ = _controller()
    assert og.update(battery=-2500, soc=95).grid == -2500
    assert og.update(battery=600, soc=95).grid == 600  # discharging: the car backs off
    # a generator charging the battery is no surplus, and gets no extra offer either
    st = og.update(battery=-2800, soc=95, generator=3000, frequency=51.0)
    assert (st.grid, st.state) == (200, "surplus")
    assert og.update(battery=None, soc=95).grid is None  # no reading: the Wattpilot pauses


def test_frequency_shows_held_back_solar() -> None:
    og, _ = _controller(offer_w=1500)
    assert og.update(battery=-100, soc=95, frequency=50.05).grid == -100
    st = og.update(battery=-100, soc=95, frequency=50.8)
    assert (st.grid, st.state) == (-1600, "offering")
    assert og.update(battery=0, soc=95, frequency=60.1).state == "surplus"  # 60 Hz system: limit 60.2
    assert og.update(battery=0, soc=95, frequency=60.5).state == "offering"

    og, _ = _controller(throttle_hz=51.5)
    assert og.update(battery=0, soc=95, frequency=51.0).state == "surplus"
    assert og.update(battery=0, soc=95, frequency=51.6).state == "offering"


def test_without_frequency_a_full_battery_with_solar_counts_as_held_back() -> None:
    og, _ = _controller(full_soc=98, offer_w=1400)
    assert og.update(battery=0, soc=99, pv=3000).grid == -1400
    assert og.update(battery=0, soc=99, pv=50).state == "surplus"  # night
    assert og.update(battery=0, soc=99).state == "surplus"  # no solar reading
    assert og.update(battery=0, soc=96, pv=3000).state == "surplus"  # not full


def test_backs_off_when_an_offer_drains_the_battery() -> None:
    og, clock = _controller(offer_w=1500)
    assert og.update(battery=0, soc=99, pv=2000).state == "offering"
    clock.now += 10  # the car took more than solar could give
    st = og.update(battery=900, soc=99, pv=2000)
    assert (st.grid, st.state) == (900, "backing_off")  # import: the Wattpilot turns down
    clock.now += BACKOFF_S - 20
    assert og.update(battery=0, soc=99, pv=2000).state == "backing_off"
    clock.now += 30
    assert og.update(battery=0, soc=99, pv=2000).state == "offering"

    # a discharge long after the last offer is just the house, not the offer's result
    clock.now += 120
    st = og.update(battery=900, soc=99, pv=2000)
    assert (st.grid, st.state) == (900, "surplus")
    assert og.update(battery=0, soc=99, pv=2000).state == "offering"


def _raw(**off_grid) -> dict:
    return {
        "name": "cabin",
        "sources": {"s": {"type": "http_json", "url": "http://x", "fields": {"b": "b"}}},
        "values": {"pv": "s.pv", "battery": "s.bat", "soc": "s.soc", "grid": "s.gen", "frequency": "s.hz"},
        "off_grid": {"enabled": True, **off_grid},
    }


def test_config() -> None:
    raw = _raw()
    del raw["values"]["grid"]
    cfg = parse(raw)  # no grid needed off grid
    assert cfg.off_grid.enabled and cfg.off_grid.car_start_soc == 90 and cfg.off_grid.throttle_hz is None
    assert parse(_raw(throttle_hz="51.5", offer_w=2000)).off_grid.throttle_hz == 51.5

    for bad, message in (
        ({"car_start_soc": 70, "car_stop_soc": 80}, "stop charge"),
        ({"throttle_hz": 5}, "45–65 Hz"),
        ({"offer_w": "lots"}, "expected a number"),
    ):
        with pytest.raises(ConfigError, match=message):
            parse(_raw(**bad))
    raw = _raw()
    del raw["values"]["soc"]
    with pytest.raises(ConfigError, match="battery charge"):
        parse(raw)
    raw = _raw()
    raw["off_grid"]["enabled"] = False
    del raw["values"]["grid"]
    with pytest.raises(ConfigError, match="grid power is required"):
        parse(raw)
    with pytest.raises(ConfigError, match="not a configured power value"):
        parse(_raw() | {"meters": [{"name": "m", "value": "frequency"}]})


def test_bridge_shows_the_wattpilot_the_made_up_grid(tmp_path: Path) -> None:
    bridge = Bridge(parse(_raw()), tmp_path)
    src = bridge.sources["s"]
    for field, value in {"pv": 5000.0, "bat": -3500.0, "soc": 93.0, "gen": 0.0, "hz": 50.0}.items():
        src.set(field, value)
    bridge.compute()
    assert bridge.values["load"] == 1500.0  # generator + solar + battery
    assert bridge.data["P_Grid"] == -3500.0  # battery charging, shown as export
    assert (bridge.data["P_Akku"], bridge.data["SOC"]) == (None, None)  # hidden from the Wattpilot
    assert bridge.data["P_Load"] == -1500.0  # still balances: grid + pv + load = 0
    assert bridge.state()["off_grid"]["state"] == "surplus"
    assert bridge.values["battery"] == -3500.0  # the power flow page keeps the real values

    src.set("hz", 51.2)  # the battery inverter holds solar back
    bridge.compute()
    assert bridge.data["P_Grid"] == -5000.0
    assert bridge.state()["off_grid"]["reason"].startswith("frequency 51.20 Hz")

