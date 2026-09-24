# Solar Bridge — Project Context

Standalone (no Home Assistant) Fronius-compatible bridge. Sister project of the HA integration
https://github.com/l2smith2/fronius-virtual-inverter — the protocol code in `solar_bridge.core`
was extracted from it and must stay behaviourally identical (the Wattpilot/SnapIN facts below were
confirmed on real hardware there).

## Layout
- `src/solar_bridge/core/` — protocol emulation, **only depends on aiohttp** (loose range) so the HA
  integration can later depend on this package from PyPI. No YAML, MQTT, Tesla or HA imports here.
  - `solar_api.py` SolarApiServer — Fronius Solar API v1 (Wattpilot). `app` attr lets callers add routes.
  - `modbus.py` ModbusTcpServer + SunSpecMeter — Smart Meter IP, SunSpec model 213, many unit IDs per port.
  - `mdns.py` RawMDNSAnnouncer — `_Fronius-SE-Inverter/SmartMeter._tcp` from UDP port 5353 (Wattpilot drops others).
  - `meter.py` per-phase model shared by HTTP and Modbus. `energy.py` counters (caller persists snapshot()).
- `src/solar_bridge/app/` — standalone app: `config.py` (YAML), `sources/` (tesla, http_json, mqtt, modbus),
  `bridge.py` (poll → convert → serve, energy.json persistence), `static/index.html` (power-flow page),
  `__main__.py` (`solar-bridge --config … [--check]`).
- `deploy/` — example config, systemd unit (CAP_NET_BIND_SERVICE, DynamicUser), `install.sh`. `Dockerfile` needs host networking.

## Sign conventions
- Config/`values`/web page use natural signs: grid + importing, pv + producing, battery + discharging, load + consuming.
- `Bridge.data` is Fronius Solar API: P_Load negated (consumption negative); P_Akku + discharging.
- Modbus meter W: + = from the grid side into the device. role `generator` negates (discharging battery reads negative).
- `load` defaults to grid + pv + battery when not configured.

## Confirmed Fronius facts (from the HA project)
- Wattpilot polls `GetMeterRealtimeData.cgi?Scope=Device` → Body.Data must be flat; System scope → `{"0": …}`.
- W register at wire address 40097; Hz float at 40095; unit 240 accepted by a SnapIN.
- Python 3.12+: `Server.wait_closed()` waits for clients and Fronius never disconnects → ModbusTcpServer.stop() closes clients first.
- mDNS packets must stay < 400 bytes; answer from source port 5353, IPv4 and IPv6.

## Testing
- `pip install -e ".[app,test]" && pytest` — core protocol tests (ported from the HA repo), sources against local
  servers (the modbus source reads our own ModbusTcpServer), end-to-end bridge run.
- CI: Python 3.11 (Raspberry Pi OS Bookworm) and 3.13. Tags `v*` publish to PyPI (trusted publishing).
