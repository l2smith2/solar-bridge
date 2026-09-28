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
- `src/solar_bridge/app/` — standalone app:
  - `web.py` App: owns the web routes + settings API, (re)creates a `Bridge` when settings are saved (no process
    restart); falls back to setup mode if the new settings can't open their port, so the page stays reachable.
  - `bridge.py` Bridge: one running configuration (sources → values → Solar API / Modbus / mDNS, energy.json).
    `config=None` = setup mode (web page only). Non-fatal start problems go in `problems` (shown on the page).
  - `config.py` parse(dict) — same schema from the page's JSON or an optional YAML file. Values may be a list of
    `source.field` (added up; missing parts skipped, None only if all missing).
  - `sources/` tesla (add-on), http_json, mqtt, modbus (own minimal client), sma_speedwire (SMA Energy Meter /
    Home Manager multicast). `read_fields()` = one bad field doesn't drop the others; JSON null = no reading, not an
    error. Sources with fixed readings declare `READINGS` (the page's `FIXED_FIELDS` must match — tested); push
    sources set `push = True` so the page's Test waits for data. `config.SOURCE_TYPES` must list every type (tested).
  - Modbus "not available" markers (0x8000, 0xFFFF, 0x80000000, 0xFFFFFFFF, float NaN — SunSpec and SMA) = no
    reading, or the field's `nan` value (templates use `nan: 0` for SMA power: inverters report NaN asleep).
  - `templates.py` device templates for the page (`{placeholders}` filled by the page from `ask` fields; an `ask`
    key not used as a placeholder becomes a source setting). To subtract a reading, give the field `scale: -1`.
    Register maps and signs checked against vendor docs / evcc / the HA integrations:
    Sigenergy plant (unit 247, input regs): 30005 grid (+ import), 30035 PV, 30037 ESS (+ charging), 30014 SOC ×0.1.
    SMA (unit 3): 30775 AC W (battery inverters: + discharging), 30773/30961/30967 DC W, 31393/31395 charge/discharge
    (U32), 30845 SOC (U32), 30865/30867 grid import/export (S32). Selectronic select.live `…/devices/<id>/point`:
    grid_w + import, battery_w + discharging, load_w, solarinverter_w (AC solar), shunt_w (DC solar on shunt 1).
    SMA Speedwire: protocol 0x6069 (and 0x6081 = HM 2.0 fw 2.07 unicast, 2 more header bytes); OBIS index 1/2 total
    import/export, 21/22, 41/42, 61/62 per phase, 0.1 W.
  - `addons.py` optional packages pip-installed into `<data_dir>/addons` from the page (the only writable place
    under the hardened systemd unit), appended to sys.path — no restart.
  - `static/index.html` power flow; `static/settings.html` settings (plain JS, no build, DOM built with h(), no innerHTML
    of user text).
- Settings precedence: `--config FILE` > `$SOLAR_BRIDGE_CONFIG` > `/etc/solar-bridge/config.yaml` if present >
  `<data_dir>/config.json` (written by the page, mode 600). File-managed settings are read-only in the page.
- Secrets (password, gw_pwd, token) are masked as `••••••••` in GET /api/config and restored on save.
  Optional settings password: pbkdf2 hash in config; page sends Basic auth itself (no WWW-Authenticate popup).
- Extras: `app` (aiomqtt — light, pure Python; fine on a Pi 2), `tesla` (pypowerwall, heavy), `yaml`, `all`.
- `deploy/` — `install.sh [--tesla] [--yaml]`, systemd unit (CAP_NET_BIND_SERVICE, DynamicUser, StateDirectory),
  optional example YAML. `Dockerfile` (`--build-arg EXTRAS=app,tesla`) needs host networking.

## Off grid (`offgrid.py`)
- `off_grid.enabled`: grid value optional (= generator / AC input), battery + soc required. The Wattpilot gets
  P_Grid = generator + battery (charging = export) or, while the battery comes first (SOC hysteresis
  car_start_soc/car_stop_soc), house load as import; minus offer_w while solar is held back (frequency ≥
  throttle_hz, default nominal + 0.2; else SOC ≥ full_soc with PV ≥ 200 W). A discharge within 60 s of an offer
  → no offers for 300 s. P_Akku/SOC are hidden from the Wattpilot (its own battery rules would double count);
  `values`/meters/energy counters keep the real readings. Untested on a real Wattpilot off grid.
- `frequency` is a value (Hz) like the others; not allowed as a Smart Meter IP power.

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
- The Solar API error middleware must re-raise `web.HTTPException` — otherwise every 404 (favicon, scanners)
  became a logged 500. (The HA integration's copy has the same bug.)

## Testing
- `pip install -e ".[app,test]" && pytest` — core protocol tests (ported from the HA repo), sources against local
  servers (the modbus source reads our own ModbusTcpServer), templates, `test_devices.py` (brand templates end to
  end against fake Modbus/HTTP/UDP devices, incl. SMA at night), and `test_web.py` driving the real app over
  HTTP: setup from scratch, save/reload, secrets, password, busy-port fallback, add-on install (pip faked).
- Manual UI check: `solar-bridge --data-dir ./data --port 8080`, open /settings (Playwright + Chromium work here).
- CI: Python 3.11 (Raspberry Pi OS Bookworm) and 3.13. Tags `v*` publish to PyPI (trusted publishing).
