# Solar Bridge

Makes non-Fronius solar, battery and meter hardware look like Fronius devices, so that:

- a **Fronius Wattpilot** can do PV surplus (Eco) charging, with its own app and Eco mode, without a Fronius inverter, and
- a **real Fronius inverter** can read extra **Smart Meter IPs**, e.g. an AC-coupled Tesla Powerwall as an *external generator*, so Solar.web shows the whole house correctly.

It runs on a Raspberry Pi (or any Linux box, or Docker), needs no cloud account, and shows live power flow on a local web page.

<img src="docs/power-flow.png" alt="Power-flow page" width="320">

> Fronius, Wattpilot and Solar.web are trademarks of Fronius International GmbH. This project is not affiliated with or endorsed by Fronius.

---

## What it emulates

| For | Protocol | Details |
|---|---|---|
| Fronius Wattpilot | Fronius Solar API v1 (HTTP, port 80) + mDNS | A GEN24-style inverter with grid, PV, battery, SOC and per-phase meter data |
| Fronius inverter (SnapIN, GEN24) | Smart Meter IP — SunSpec Modbus TCP (port 502) | Any number of meters on one port, one unit ID each: grid, generator or load position |

## Where the data comes from

| Source | Covers |
|---|---|
| `tesla` | Tesla Powerwall 2 / + / 3 via [pypowerwall](https://github.com/jasonacox/pypowerwall): local gateway, Powerwall 3 TEDAPI, or cloud |
| `modbus` | Any Modbus TCP device: SunSpec inverters and meters (SMA, SolarEdge, Fronius, Kostal, …), Sungrow, Huawei, GoodWe, … |
| `mqtt` | Anything that publishes to MQTT: Victron, Node-RED, evcc, pypowerwall-server, Home Assistant, … |
| `http_json` | Any JSON HTTP API: Shelly, Enphase Envoy, OpenDTU, … |

Mix and match: e.g. grid power from a Shelly, battery from a Powerwall.

---

## Install on a Raspberry Pi

**Hardware:** any Raspberry Pi 3 or newer, the official power supply, a good microSD card, and a **network cable** to your router. Wi-Fi works for the data sources, but the Wattpilot finds the bridge by multicast, which is unreliable over Wi-Fi.

1. Flash **Raspberry Pi OS Lite** with [Raspberry Pi Imager](https://www.raspberrypi.com/software/). In its settings, set the hostname (e.g. `fronius-virtual`), a user, and enable SSH.
2. SSH in and run:
   ```sh
   curl -fsSL https://raw.githubusercontent.com/l2smith2/solar-bridge/main/deploy/install.sh | sudo sh
   ```
3. Edit the config (examples for every source are inside):
   ```sh
   sudo nano /etc/solar-bridge/config.yaml
   ```
4. Test it — this reads every source and shows exactly what will be served:
   ```sh
   sudo /opt/solar-bridge/bin/solar-bridge --check
   ```
5. Start it, and open **http://fronius-virtual.local/** for the live power flow:
   ```sh
   sudo systemctl start solar-bridge
   ```
6. Pair the Wattpilot: Solar.wattpilot app → scan for inverters → pick your display name.

It starts on boot and restarts itself if anything goes wrong. Energy totals are saved every 5 minutes and survive restarts and power cuts.

**Optional, for years of hands-off running:** `sudo raspi-config` → Performance → **Overlay file system**, which makes the SD card read-only so power cuts can't corrupt it. Energy totals then reset when the Pi reboots — leave it off if Solar.web energy history matters to you.

## Docker

```sh
docker build -t solar-bridge .
docker run -d --name solar-bridge --network host --restart unless-stopped \
  -v /path/to/config.yaml:/etc/solar-bridge/config.yaml \
  -v solar-bridge:/var/lib/solar-bridge solar-bridge
```
`--network host` is required for mDNS.

---

## Configuration

See [`deploy/config.example.yaml`](deploy/config.example.yaml). In short:

```yaml
name: fronius-virtual          # hostname → http://fronius-virtual.local
display_name: MyHome           # shown when pairing the Wattpilot
grid: {phases: 1, breaker_amps: 32}

sources:
  powerwall: {type: tesla, host: 192.168.1.50, password: "…", email: you@example.com}

values:                        # grid + importing, pv + producing,
  grid: powerwall.grid         # battery + discharging, load + consuming
  pv: powerwall.solar
  battery: powerwall.battery
  soc: powerwall.soc

meters:                        # optional, for a real Fronius inverter
  - {name: grid, unit_id: 240, value: grid, role: grid}
  - {name: ac-battery, unit_id: 241, value: battery, role: generator}
```

Signs are the everyday ones (not Fronius' internal ones — the bridge converts). If a reading comes out backwards, add `invert: true`:
`battery: {from: powerwall.battery, invert: true}`. `--check` shows every value with its meaning, so you can see straight away.

### Adding a meter on the Fronius inverter
In the inverter's web interface, add a **Fronius Smart Meter IP** at the bridge's IP address with the meter's **unit ID** as the Modbus address, and pick the position matching its `role` (feed-in point / external generator / consumption path). All meters share port 502.

A meter reports power flowing *from the grid side into the device* as positive, like a real meter wired with the grid on one side: a discharging battery at the generator position reads negative, which Fronius shows as generation. If yours shows backwards, set `invert: true` on the meter.

---

## Web page

`http://<name>.local/` shows live solar, grid, battery and home power, plus:
- when the Wattpilot last polled, and when the Fronius inverter last read each meter
- each data source's health and last reading

`/api/state` returns the same as JSON.

## Home Assistant

Prefer Home Assistant? The [Fronius Virtual Inverter](https://github.com/l2smith2/fronius-virtual-inverter) integration does the same inside HA, using HA sensors as sources.

## Development

```sh
pip install -e ".[app,test]"
pytest
```

`solar_bridge.core` (the protocol emulation) depends only on aiohttp, so the Home Assistant integration can use it as a library. Everything else lives in `solar_bridge.app`.

## License

MIT
