"""Fronius Solar API v1 HTTP server — what the Wattpilot pairs with and polls."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from collections.abc import Callable, Mapping
from typing import Any

from aiohttp import web

from .meter import read_meter

# Values in Fronius Solar API conventions: P_Grid, P_PV, P_Akku, P_Load, SOC,
# E_Day/E_Year/E_Total, _tot_wh_imp/_exp, grid_phases, grid_ct_rating, per-phase keys
DataFn = Callable[[], Mapping[str, Any] | None]

FRONIUS_DEVICE_TYPE = 1  # GEN24 hybrid
API_BASE = "/solar_api/v1"
API_VERSION = "/solar_api/GetAPIVersion.cgi"
API_ACTIVE_DEVICE_INFO = f"{API_BASE}/GetActiveDeviceInfo.cgi"
API_POWER_FLOW = f"{API_BASE}/GetPowerFlowRealtimeData.fcgi"
API_INVERTER_INFO = f"{API_BASE}/GetInverterInfo.fcgi"
API_INVERTER_REALTIME = f"{API_BASE}/GetInverterRealtimeData.fcgi"
API_METER_REALTIME = f"{API_BASE}/GetMeterRealtimeData.fcgi"
API_METER_REALTIME_CGI = f"{API_BASE}/GetMeterRealtimeData.cgi"
API_STORAGE_REALTIME = f"{API_BASE}/GetStorageRealtimeData.fcgi"
API_LOGGER_INFO = f"{API_BASE}/GetLoggerInfo.fcgi"

_LOGGER = logging.getLogger(__name__)


@web.middleware
async def _error_middleware(request: web.Request, handler) -> web.Response:
    try:
        return await handler(request)
    except web.HTTPException:
        raise  # 404s etc. are normal answers, not server errors
    except Exception as e:
        _LOGGER.error("HTTP handler error for %s: %s", request.path, e)
        return web.Response(status=500)


def _make_head(timestamp: str | None = None, request_arguments: dict | None = None) -> dict:
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).astimezone().isoformat()
    return {
        "RequestArguments": request_arguments if request_arguments is not None else {},
        "Status": {"Code": 0, "Reason": "", "UserMessage": ""},
        "Timestamp": timestamp,
    }


class SolarApiServer:
    """Minimal Fronius Solar API v1 HTTP server."""

    def __init__(
        self,
        data: DataFn,
        port: int,
        serial: str,
        system_name: str,
        host: str = "0.0.0.0",
    ) -> None:
        self._data = data
        self._port = port
        self._host = host
        self._serial = serial
        self._system_name = system_name
        # Callers may add routes (e.g. a web page at /) before start()
        self.app = web.Application(middlewares=[_error_middleware])
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._setup_routes()

    def _setup_routes(self) -> None:
        self.app.router.add_get(API_VERSION, self._handle_api_version)
        self.app.router.add_get(API_ACTIVE_DEVICE_INFO, self._handle_active_device_info)
        self.app.router.add_get(API_POWER_FLOW, self._handle_power_flow)
        self.app.router.add_get(API_INVERTER_INFO, self._handle_inverter_info)
        self.app.router.add_get(API_INVERTER_REALTIME, self._handle_inverter_realtime)
        self.app.router.add_get(API_METER_REALTIME, self._handle_meter_realtime)
        self.app.router.add_get(API_METER_REALTIME_CGI, self._handle_meter_realtime)
        self.app.router.add_get(API_STORAGE_REALTIME, self._handle_storage_realtime)
        self.app.router.add_get(API_LOGGER_INFO, self._handle_logger_info)
        # Catch-all for any other Solar API paths
        self.app.router.add_get("/solar_api/{tail:.*}", self._handle_unknown)

    async def start(self) -> None:
        """Start the HTTP server."""
        self._runner = web.AppRunner(self.app, access_log=None, shutdown_timeout=5)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        _LOGGER.info(
            "Solar API server started on port %d", self._port
        )

    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._runner:
            await self._runner.cleanup()
        _LOGGER.info("Solar API server stopped")

    def _json_response(self, data: Any) -> web.Response:
        return web.Response(
            content_type="application/json",
            text=json.dumps(data, indent=3),
        )

    async def _handle_api_version(self, request: web.Request) -> web.Response:
        return self._json_response(
            {
                "APIVersion": 1,
                "BaseURL": "/solar_api/v1/",
                "CompatibilityRange": "1.8-1",
            }
        )

    async def _handle_power_flow(self, request: web.Request) -> web.Response:
        data = (self._data() or {})
        timestamp = datetime.now(timezone.utc).astimezone().isoformat()

        p_pv = data.get("P_PV")
        p_grid = data.get("P_Grid")
        p_akku = data.get("P_Akku")
        p_load = data.get("P_Load")
        soc = data.get("SOC")

        e_day = data.get("E_Day", 0.0)
        e_year = data.get("E_Year", 0.0)
        e_total = data.get("E_Total", 0.0)

        inverter_block: dict[str, Any] = {
            "DT": 102,
            "P": round(p_pv, 1) if p_pv is not None else None,
            "E_Day": round(e_day, 1),
            "E_Year": round(e_year, 1),
            "E_Total": round(e_total, 1),
        }
        if soc is not None:
            inverter_block["SOC"] = round(soc, 1)
            inverter_block["Battery_Mode"] = "normal"

        site_block: dict[str, Any] = {
            "E_Day": round(e_day, 1),
            "E_Year": round(e_year, 1),
            "E_Total": round(e_total, 1),
            "Meter_Location": "grid",
            "Mode": "bidirectional" if p_akku is not None else "meter",
            "P_PV": round(p_pv, 1) if p_pv is not None else None,
            "P_Grid": round(p_grid, 1) if p_grid is not None else None,
            "P_Akku": round(p_akku, 1) if p_akku is not None else None,
        }

        if p_akku is not None:
            site_block["BatteryStandby"] = False
            site_block["BackupMode"] = False

        if p_load is not None:
            site_block["P_Load"] = round(p_load, 1)

        # Fronius definitions: autonomy = share of load not imported;
        # self-consumption = share of PV not exported
        if p_grid is not None:
            if p_load is not None and p_load < 0:
                site_block["rel_Autonomy"] = (
                    100.0 if p_grid <= 0 else round(max(0.0, (1 + p_grid / p_load) * 100), 1)
                )
            if p_pv is not None and p_pv > 0:
                site_block["rel_SelfConsumption"] = (
                    100.0 if p_grid >= 0 else round(max(0.0, (1 + p_grid / p_pv) * 100), 1)
                )

        payload = {
            "Body": {
                "Data": {
                    "Inverters": {"1": inverter_block},
                    "Site": site_block,
                    "Version": "12",
                }
            },
            "Head": _make_head(timestamp),
        }
        return self._json_response(payload)

    async def _handle_active_device_info(self, request: web.Request) -> web.Response:
        device_class = request.rel_url.query.get("DeviceClass", "")
        if device_class == "Meter":
            data = {"0": {"DT": -1, "Serial": self._serial}}
        else:
            data = {}
        payload = {
            "Body": {"Data": data},
            "Head": _make_head(),
        }
        return self._json_response(payload)

    async def _handle_inverter_info(self, request: web.Request) -> web.Response:
        ct_rating = (self._data() or {}).get("grid_ct_rating", 32)
        payload = {
            "Body": {
                "Data": {
                    "1": {
                        "CustomName": self._system_name,
                        "DT": FRONIUS_DEVICE_TYPE,
                        "ErrorCode": 0,
                        "PVPower": 5000,
                        "Show": 1,
                        "StatusCode": 7,  # 7 = running
                        "UniqueID": self._serial,
                        "MaxACCurrent": ct_rating,
                    }
                }
            },
            "Head": _make_head(),
        }
        return self._json_response(payload)

    async def _handle_inverter_realtime(self, request: web.Request) -> web.Response:
        data = (self._data() or {})
        p_pv = data.get("P_PV", 0.0) or 0.0

        payload = {
            "Body": {
                "Data": {
                    "PAC": {"Value": round(p_pv, 1), "Unit": "W"},
                    "SAC": {"Value": round(p_pv, 1), "Unit": "VA"},
                    "DAY_ENERGY": {"Value": round(data.get("E_Day", 0.0), 1), "Unit": "Wh"},
                    "YEAR_ENERGY": {"Value": round(data.get("E_Year", 0.0), 1), "Unit": "Wh"},
                    "TOTAL_ENERGY": {"Value": round(data.get("E_Total", 0.0), 1), "Unit": "Wh"},
                }
            },
            "Head": _make_head(),
        }
        return self._json_response(payload)

    async def _handle_meter_realtime(self, request: web.Request) -> web.Response:
        """Return grid meter data with per-phase breakdown for load balancing."""
        m = read_meter((self._data() or {}))
        ph1 = m.phases[0]

        # Field order matches the response the Wattpilot was validated against
        meter_data: dict[str, Any] = {
            "Details": {
                "Manufacturer": "Fronius",
                "Model": "Smart Meter TS 65A-3",
                "Serial": self._serial,
            },
            "Enable": 1,
            "TimeStamp": int(datetime.now(timezone.utc).timestamp()),
            "Meter_Location_Current": 0,
            "Visible": 1,
            "Frequency_Phase_Average": 50.0,
            "PowerReal_P_Sum": round(m.p, 1),
            "PowerReactive_Q_Sum": round(m.q, 1),
            "PowerApparent_S_Sum": round(m.s, 1),
            "PowerFactor_Sum": m.pf,
            "Current_AC_Sum": round(m.i, 2),
            "EnergyReal_WAC_Minus_Absolute": round(m.wh_exp, 1),
            "EnergyReal_WAC_Plus_Absolute": round(m.wh_imp, 1),
            "EnergyReal_WAC_Sum_Consumed": round(m.wh_imp, 1),
            "EnergyReal_WAC_Sum_Produced": round(m.wh_exp, 1),
            "EnergyReactive_VArAC_Sum_Consumed": 0.0,
            "EnergyReactive_VArAC_Sum_Produced": 0.0,
            "Current_AC_Phase_1": round(ph1.i, 2),
            "PowerReal_P_Phase_1": round(ph1.p, 1),
            "PowerReactive_Q_Phase_1": round(ph1.q, 1),
            "PowerApparent_S_Phase_1": round(ph1.s, 1),
            "PowerFactor_Phase_1": ph1.pf,
            "EnergyReal_WAC_Phase_1_Consumed": round(m.wh_imp, 1),
            "EnergyReal_WAC_Phase_1_Produced": round(m.wh_exp, 1),
            "EnergyReactive_VArAC_Phase_1_Consumed": 0.0,
            "EnergyReactive_VArAC_Phase_1_Produced": 0.0,
        }
        if ph1.v_measured is not None:
            meter_data["Voltage_AC_Phase_1"] = round(ph1.v_measured, 1)

        if len(m.phases) == 3:
            _, ph2, ph3 = m.phases
            meter_data.update({
                "Current_AC_Phase_2": round(ph2.i, 2),
                "Current_AC_Phase_3": round(ph3.i, 2),
                "PowerReal_P_Phase_2": round(ph2.p, 1),
                "PowerReal_P_Phase_3": round(ph3.p, 1),
                "PowerReactive_Q_Phase_2": round(ph2.q, 1),
                "PowerReactive_Q_Phase_3": round(ph3.q, 1),
                "PowerApparent_S_Phase_2": round(ph2.s, 1),
                "PowerApparent_S_Phase_3": round(ph3.s, 1),
                "PowerFactor_Phase_2": ph2.pf,
                "PowerFactor_Phase_3": ph3.pf,
            })
            if ph2.v_measured is not None:
                meter_data["Voltage_AC_Phase_2"] = round(ph2.v_measured, 1)
            if ph3.v_measured is not None:
                meter_data["Voltage_AC_Phase_3"] = round(ph3.v_measured, 1)

        scope = request.rel_url.query.get("Scope", "System")
        device_id = request.rel_url.query.get("DeviceId", "0")
        request_args = {
            "DeviceClass": "Meter",
            "DeviceId": int(device_id),
            "Scope": scope,
        }

        # Device scope: flat response (one device); System scope: indexed by device address
        body_data: dict = meter_data if scope == "Device" else {"0": meter_data}

        payload = {
            "Body": {"Data": body_data},
            "Head": _make_head(request_arguments=request_args),
        }
        return self._json_response(payload)

    async def _handle_storage_realtime(self, request: web.Request) -> web.Response:
        """Return battery/storage data."""
        data = (self._data() or {})
        soc = data.get("SOC")
        p_akku = data.get("P_Akku")

        if soc is None and p_akku is None:
            # No battery configured
            payload = {
                "Body": {"Data": {}},
                "Head": _make_head(),
            }
            return self._json_response(payload)

        payload = {
            "Body": {
                "Data": {
                    "0": {
                        "Controller": {
                            "Capacity_Maximum": 10000,
                            "Current_DC": 0.0,
                            "Details": {
                                "Manufacturer": "BYD",
                                "Model": "Battery-Box Premium HVS",
                                "Serial": self._serial,
                            },
                            "Enable": 1,
                            "StateOfCharge_Relative": round(soc, 1) if soc is not None else 0.0,
                            "Status_BatteryCell": "normal",
                            "Temperature_Cell": 25.0,
                            "Voltage_DC": 400.0,
                        },
                        "Modules": [],
                    }
                }
            },
            "Head": _make_head(),
        }
        return self._json_response(payload)

    async def _handle_logger_info(self, request: web.Request) -> web.Response:
        payload = {
            "Body": {
                "LoggerInfo": {
                    "UniqueID": f"240.{self._system_name}",
                    "ProductID": "fronius-datamanager-card",
                    "PlatformID": "wilma",
                    "HWVersion": "1.4E",
                    "SWVersion": "3.4.0-102",
                    "TimezoneLocation": "Australia/Sydney",
                    "TimezoneName": "AEST",
                    "UTCOffset": 36000,
                    "DefaultLanguage": "en",
                    "CashFactor": 0.0,
                    "CashCurrency": "AUD",
                    "CO2Factor": 0.53,
                    "CO2Unit": "kg",
                    "Systemname": self._system_name,
                }
            },
            "Head": _make_head(),
        }
        return self._json_response(payload)

    async def _handle_unknown(self, request: web.Request) -> web.Response:
        _LOGGER.debug("Unknown Solar API request: %s", request.path)
        payload = {
            "Body": {"Data": {}},
            "Head": _make_head(),
        }
        return self._json_response(payload)
