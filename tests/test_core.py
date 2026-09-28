"""Solar API server, meter model and energy counters."""
from __future__ import annotations

from datetime import datetime

import aiohttp
import pytest

from solar_bridge.core import EnergyCounters, SolarApiServer, read_meter
from solar_bridge.core.energy import MAX_GAP


async def _get(port: int, path: str) -> dict:
    async with aiohttp.ClientSession() as session, session.get(f"http://127.0.0.1:{port}{path}") as resp:
        assert resp.status == 200
        return await resp.json(content_type=None)


async def test_solar_api_endpoints(free_port: int) -> None:
    data = {
        "P_Grid": -1500.0, "P_PV": 4000.0, "P_Akku": -2000.0, "P_Load": -500.0, "SOC": 81.0,
        "E_Day": 1000.0, "E_Year": 2000.0, "E_Total": 3000.0, "_tot_wh_imp": 5.0, "_tot_wh_exp": 6.0,
        "grid_phases": 1, "grid_ct_rating": 40.0,
    }
    server = SolarApiServer(lambda: data, free_port, "MyHome", "MyHome", host="127.0.0.1")
    await server.start()
    try:
        flow = await _get(free_port, "/solar_api/v1/GetPowerFlowRealtimeData.fcgi")
        site = flow["Body"]["Data"]["Site"]
        assert (site["P_Grid"], site["P_PV"], site["P_Akku"], site["P_Load"]) == (-1500.0, 4000.0, -2000.0, -500.0)
        assert site["Mode"] == "bidirectional"
        assert site["rel_Autonomy"] == 100.0
        assert site["rel_SelfConsumption"] == 62.5
        assert flow["Body"]["Data"]["Inverters"]["1"]["SOC"] == 81.0

        device = await _get(free_port, "/solar_api/v1/GetMeterRealtimeData.cgi?Scope=Device&DeviceId=0")
        assert device["Body"]["Data"]["PowerReal_P_Sum"] == -1500.0  # Device scope: flat
        assert device["Head"]["RequestArguments"] == {"DeviceClass": "Meter", "DeviceId": 0, "Scope": "Device"}
        system = await _get(free_port, "/solar_api/v1/GetMeterRealtimeData.fcgi")
        assert system["Body"]["Data"]["0"]["EnergyReal_WAC_Sum_Produced"] == 6.0

        info = await _get(free_port, "/solar_api/v1/GetInverterInfo.fcgi")
        assert info["Body"]["Data"]["1"]["MaxACCurrent"] == 40.0
        logger = await _get(free_port, "/solar_api/v1/GetLoggerInfo.fcgi")
        assert logger["Body"]["LoggerInfo"]["UniqueID"] == "240.MyHome"
        assert (await _get(free_port, "/solar_api/GetAPIVersion.cgi"))["CompatibilityRange"] == "1.8-1"
        async with aiohttp.ClientSession() as session, session.get(f"http://127.0.0.1:{free_port}/favicon.ico") as r:
            assert r.status == 404  # not a 500 with an error logged
    finally:
        await server.stop()


async def test_solar_api_with_no_data_yet(free_port: int) -> None:
    server = SolarApiServer(lambda: None, free_port, "x", "x", host="127.0.0.1")
    await server.start()
    try:
        site = (await _get(free_port, "/solar_api/v1/GetPowerFlowRealtimeData.fcgi"))["Body"]["Data"]["Site"]
        assert site["P_Grid"] is None  # the Wattpilot then pauses surplus charging
    finally:
        await server.stop()


def test_meter_three_phase_split_and_measured_phases() -> None:
    m = read_meter({"P_Grid": 3000.0, "grid_phases": 3})
    assert [ph.p for ph in m.phases] == [1000.0, 1000.0, 1000.0]
    assert m.phases[0].i == pytest.approx(1000 / 240)

    m = read_meter({"P_Grid": 500.0, "grid_phases": 1, "I_Grid_A": 5.0, "V_Grid_A": 230.0, "PF_Grid_A": 0.9})
    ph = m.phases[0]
    assert (ph.i, ph.s, ph.pf, ph.v_measured) == (5.0, 1150.0, 0.9, 230.0)


def test_energy_counters() -> None:
    clock = [0.0]
    now = [datetime(2026, 9, 1, 12, 0).astimezone()]
    counters = EnergyCounters(now=lambda: now[0], clock=lambda: clock[0])
    counters.update(1000.0, -500.0)  # first call only starts the clock
    clock[0] = 300.0
    values = counters.update(1000.0, -500.0)
    assert values["E_Day"] == pytest.approx(1000 * 300 / 3600)
    assert values["_tot_wh_exp"] == pytest.approx(500 * 300 / 3600)
    assert values["_tot_wh_imp"] == 0.0

    clock[0] += 10 * MAX_GAP  # a stall never counts more than MAX_GAP
    values = counters.update(0.0, 3600.0)
    assert values["_tot_wh_imp"] == pytest.approx(3600 * MAX_GAP / 3600)

    # Restored counters continue; a new day resets E_Day only
    restored = EnergyCounters(now=lambda: now[0], clock=lambda: clock[0])
    restored.restore(counters.snapshot())
    now[0] = datetime(2026, 9, 2, 0, 1).astimezone()
    values = restored.update(None, None)
    assert values["E_Day"] == 0.0
    assert values["E_Total"] == counters.values["E_Total"]
    assert values["_tot_wh_imp"] == counters.values["_tot_wh_imp"]
