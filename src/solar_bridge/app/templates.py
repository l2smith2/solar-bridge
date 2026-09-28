"""Ready-made device templates for the settings page.

A template fills in a source config (strings may contain {placeholders} for the
`ask` fields) and suggests which of its readings feed which value. Templates are
starting points: the page's live test shows the real readings before saving.

values: value name → field, list of fields (added up), or {"from": ..., "invert": true}.
To subtract a reading (e.g. charging from discharging), give that field scale -1.
"""
from __future__ import annotations

from typing import Any

HOST = {"key": "host", "label": "IP address or hostname", "required": True}
SMA_UNIT = {"key": "unit", "label": "Modbus unit ID", "default": 3}
SMA_NOTE = "Switch on Modbus TCP in the inverter's web interface first (Device parameters → " \
    "External communication → Modbus → TCP server)."

TEMPLATES: list[dict[str, Any]] = [
    # ── Batteries and hybrid systems ─────────────────────────────────────────
    {
        "id": "tesla",
        "group": "Batteries & inverters",
        "label": "Tesla Powerwall 2 / + / 3",
        "addon": "tesla",
        "ask": [
            HOST,
            {"key": "password", "label": "Gateway customer password", "secret": True},
            {"key": "email", "label": "Email (as used with the gateway)"},
            {"key": "gw_pwd", "label": "Powerwall 3 only: gateway Wi-Fi password", "secret": True},
            {"key": "timezone", "label": "Time zone", "default": "browser"},
        ],
        "source": {"type": "tesla"},
        "values": {"grid": "grid", "pv": "solar", "battery": "battery", "soc": "soc", "load": "load"},
        "note": "Needs the Tesla add-on (installed with one click). Reads the gateway on your network.",
    },
    {
        "id": "victron-gx",
        "group": "Batteries & inverters",
        "label": "Victron GX (Cerbo GX, Venus OS) — Modbus TCP",
        "ask": [HOST],
        "source": {
            "type": "modbus", "host": "{host}", "port": 502, "unit": 100,
            "fields": {
                "grid_l1": {"address": 820, "type": "int16"},
                "grid_l2": {"address": 821, "type": "int16"},
                "grid_l3": {"address": 822, "type": "int16"},
                "battery_charge": {"address": 842, "type": "int16"},
                "soc": {"address": 843, "type": "uint16"},
                "pv_dc": {"address": 850, "type": "uint16"},
            },
        },
        "values": {
            "grid": ["grid_l1", "grid_l2", "grid_l3"],
            "battery": {"from": "battery_charge", "invert": True},  # Victron: + charging
            "soc": "soc",
            "pv": "pv_dc",
        },
        "note": "Enable Modbus TCP in the GX settings. Uses the system registers (unit 100); "
        "check the live values, and add AC-coupled PV (registers 808-810) if you have it.",
    },
    {
        "id": "sigenergy",
        "group": "Batteries & inverters",
        "label": "Sigenergy SigenStor / Sigen hybrid — Modbus TCP",
        "ask": [HOST],
        "source": {
            "type": "modbus", "host": "{host}", "port": 502, "unit": 247,
            "fields": {
                "grid": {"address": 30005, "type": "int32", "input": True},
                "pv": {"address": 30035, "type": "int32", "input": True},
                "battery_charge": {"address": 30037, "type": "int32", "input": True},
                "soc": {"address": 30014, "type": "uint16", "input": True, "scale": 0.1},
            },
        },
        "values": {
            "grid": "grid",
            "pv": "pv",
            "battery": {"from": "battery_charge", "invert": True},  # Sigenergy: + charging
            "soc": "soc",
        },
        "note": "Use the Sigenergy inverter's IP address. Reads the whole system (plant address 247). "
        "Your installer has to switch on Modbus TCP (it isn't in the customer app). "
        "Other brands' solar inverters on the AC side: add register 30194 (int32, input) to Solar power.",
    },
    {
        "id": "sma-hybrid",
        "group": "Batteries & inverters",
        "label": "SMA hybrid inverter (Sunny Tripower / Sunny Boy Smart Energy) — Modbus TCP",
        "ask": [HOST, SMA_UNIT],
        "source": {
            "type": "modbus", "host": "{host}", "port": 502, "unit": "{unit}",
            "fields": {
                "pv_a": {"address": 30773, "type": "int32", "input": True, "nan": 0},
                "pv_b": {"address": 30961, "type": "int32", "input": True, "nan": 0},
                "pv_c": {"address": 30967, "type": "int32", "input": True, "nan": 0},
                "battery_discharge": {"address": 31395, "type": "uint32", "input": True, "nan": 0},
                "battery_charge": {"address": 31393, "type": "uint32", "input": True, "nan": 0, "scale": -1},
                "soc": {"address": 30845, "type": "uint32", "input": True},
                "grid_import": {"address": 30865, "type": "int32", "input": True},
                "grid_export": {"address": 30867, "type": "int32", "input": True, "scale": -1},
            },
        },
        "values": {
            "pv": ["pv_a", "pv_b", "pv_c"],
            "battery": ["battery_discharge", "battery_charge"],  # charging reads negative
            "soc": "soc",
            "grid": ["grid_import", "grid_export"],  # exporting reads negative
        },
        "note": SMA_NOTE + " Grid readings need an SMA Energy Meter or Home Manager connected to the "
        "inverter; or add that meter as its own device (under Energy meters).",
    },
    {
        "id": "sma-battery",
        "group": "Batteries & inverters",
        "label": "SMA battery inverter (Sunny Boy Storage, Sunny Island) — Modbus TCP",
        "ask": [HOST, SMA_UNIT],
        "source": {
            "type": "modbus", "host": "{host}", "port": 502, "unit": "{unit}",
            "fields": {
                "battery": {"address": 30775, "type": "int32", "input": True, "nan": 0},  # AC power
                "soc": {"address": 30845, "type": "uint32", "input": True},
                "frequency": {"address": 30803, "type": "uint32", "input": True, "scale": 0.01},
            },
        },
        "values": {"battery": "battery", "soc": "soc", "frequency": "frequency"},
        "note": SMA_NOTE + " For grid readings, add the SMA Energy Meter as a device too (under Energy meters).",
    },
    {
        "id": "selectronic",
        "group": "Batteries & inverters",
        "label": "Selectronic SP PRO (select.live on your network)",
        "ask": [
            {"key": "host", "label": "select.live IP address", "required": True},
            {"key": "device", "label": "Device ID (shown on the select.live display)", "required": True},
        ],
        "source": {
            "type": "http_json",
            "url": "http://{host}/cgi-bin/solarmonweb/devices/{device}/point",
            "fields": {
                "grid": "items.grid_w",
                "battery": "items.battery_w",
                "soc": "items.battery_soc",
                "load": "items.load_w",
                "solar_ac": "items.solarinverter_w",
                "solar_dc": "items.shunt_w",
            },
        },
        "values": {
            "grid": "grid", "battery": "battery", "soc": "soc", "load": "load", "pv": ["solar_ac", "solar_dc"],
        },
        "note": "Reads the select.live box on your network, with no cloud login. Solar power adds the AC-coupled "
        "inverter (solar_ac) and DC solar on shunt 1 (solar_dc); remove solar_dc if shunt 1 measures something "
        "else. Also works with selpi: enter its address with the port, e.g. 192.168.1.30:8000.",
    },
    {
        "id": "fronius-solar-api",
        "group": "Batteries & inverters",
        "label": "Fronius inverter (Solar API) — e.g. a SnapIN without battery support",
        "ask": [HOST],
        "source": {
            "type": "http_json",
            "url": "http://{host}/solar_api/v1/GetPowerFlowRealtimeData.fcgi",
            "fields": {
                "grid": "Body.Data.Site.P_Grid",
                "pv": "Body.Data.Site.P_PV",
                "load": "Body.Data.Site.P_Load",
            },
        },
        "values": {"grid": "grid", "pv": "pv", "load": {"from": "load", "invert": True}},
        "note": "Combine with a battery device to give the Wattpilot the whole picture.",
    },
    {
        "id": "sunspec-inverter",
        "group": "Batteries & inverters",
        "label": "SunSpec inverter — Modbus TCP (SolarEdge, Fronius, Kostal, …)",
        "ask": [HOST, {"key": "unit", "label": "Modbus unit ID", "default": 1}],
        "source": {
            "type": "modbus", "host": "{host}", "port": 502, "unit": "{unit}",
            "fields": {"pv": {"address": 40083, "type": "int16", "scale_register": 40084}},
        },
        "values": {"pv": "pv"},
        "note": "Standard layout (inverter model right after the common block at 40001). "
        "Enable Modbus TCP on the inverter first.",
    },
    # ── Meters ───────────────────────────────────────────────────────────────
    {
        "id": "shelly-pro-3em",
        "group": "Energy meters",
        "label": "Shelly Pro 3EM / 3EM-63 (Gen2 and later)",
        "ask": [HOST],
        "source": {"type": "http_json", "url": "http://{host}/rpc/EM.GetStatus?id=0",
                   "fields": {"grid": "total_act_power"}},
        "values": {"grid": "grid"},
        "note": "Clamps on the grid connection: positive = importing.",
    },
    {
        "id": "shelly-3em",
        "group": "Energy meters",
        "label": "Shelly 3EM (Gen1)",
        "ask": [HOST],
        "source": {"type": "http_json", "url": "http://{host}/status", "fields": {"grid": "total_power"}},
        "values": {"grid": "grid"},
    },
    {
        "id": "shelly-em1",
        "group": "Energy meters",
        "label": "Shelly Pro EM / EM Gen3 (one channel)",
        "ask": [HOST, {"key": "channel", "label": "Channel (0 or 1)", "default": 0}],
        "source": {"type": "http_json", "url": "http://{host}/rpc/EM1.GetStatus?id={channel}",
                   "fields": {"power": "act_power"}},
        "values": {},
        "note": "Then choose whether this channel measures the grid or solar.",
    },
    {
        "id": "sma-energy-meter",
        "group": "Energy meters",
        "label": "SMA Energy Meter / Sunny Home Manager 2.0",
        "ask": [{"key": "serial", "label": "Serial number (only needed with several SMA meters)"}],
        "source": {"type": "sma_speedwire"},
        "values": {"grid": "grid"},
        "note": "Picks up the readings the meter sends on your network every second: nothing to set up, "
        "but it must be on the same network as this device.",
    },
    {
        "id": "enphase-envoy",
        "group": "Energy meters",
        "label": "Enphase IQ Gateway / Envoy (local API)",
        "ask": [HOST, {"key": "token", "label": "Access token (from entrez.enphaseenergy.com)", "secret": True}],
        "source": {
            "type": "http_json", "url": "https://{host}/production.json?details=1", "token": "{token}",
            "fields": {"pv": "production.1.wNow", "grid": "consumption.1.wNow", "load": "consumption.0.wNow"},
        },
        "values": {"pv": "pv", "grid": "grid", "load": "load"},
        "note": "Grid and home values need consumption CTs installed on the gateway.",
    },
    # ── Solar ────────────────────────────────────────────────────────────────
    {
        "id": "sma-inverter",
        "group": "Solar",
        "label": "SMA solar inverter (Sunny Boy, Sunny Tripower) — Modbus TCP",
        "ask": [HOST, SMA_UNIT],
        "source": {
            "type": "modbus", "host": "{host}", "port": 502, "unit": "{unit}",
            "fields": {"pv": {"address": 30775, "type": "int32", "input": True, "nan": 0}},  # 0 W asleep
        },
        "values": {"pv": "pv"},
        "note": SMA_NOTE,
    },
    {
        "id": "opendtu",
        "group": "Solar",
        "label": "OpenDTU / AhoyDTU-compatible (Hoymiles micro-inverters)",
        "ask": [HOST],
        "source": {"type": "http_json", "url": "http://{host}/api/livedata/status",
                   "fields": {"pv": "total.Power.v"}},
        "values": {"pv": "pv"},
    },
    {
        "id": "shelly-switch",
        "group": "Solar",
        "label": "Shelly plug / switch with power metering (plug-in solar)",
        "ask": [HOST],
        "source": {"type": "http_json", "url": "http://{host}/rpc/Switch.GetStatus?id=0",
                   "fields": {"pv": "apower"}},
        "values": {"pv": "pv"},
    },
    # ── Anything else ────────────────────────────────────────────────────────
    {
        "id": "mqtt",
        "group": "Other devices",
        "label": "Any device via MQTT (Victron, Node-RED, evcc, Home Assistant, …)",
        "ask": [HOST, {"key": "port", "label": "Port", "default": 1883},
                {"key": "username", "label": "Username"}, {"key": "password", "label": "Password", "secret": True}],
        "source": {"type": "mqtt", "host": "{host}", "port": "{port}", "fields": {}},
        "values": {},
        "note": "Then list the topics to read, e.g. grid → home/grid/power.",
    },
    {
        "id": "modbus",
        "group": "Other devices",
        "label": "Any device via Modbus TCP",
        "ask": [HOST, {"key": "port", "label": "Port", "default": 502}, {"key": "unit", "label": "Unit ID", "default": 1}],
        "source": {"type": "modbus", "host": "{host}", "port": "{port}", "unit": "{unit}", "fields": {}},
        "values": {},
        "note": "Then list the registers from the device's Modbus documentation.",
    },
    {
        "id": "http_json",
        "group": "Other devices",
        "label": "Any device with a JSON web API",
        "ask": [{"key": "url", "label": "URL", "required": True}],
        "source": {"type": "http_json", "url": "{url}", "fields": {}},
        "values": {},
        "note": "Then list where each reading is in the JSON, e.g. data.power.",
    },
]
