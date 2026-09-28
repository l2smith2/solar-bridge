"""Command line.

    solar-bridge                      settings from the web page (http://<device>/settings)
    solar-bridge --config FILE.yaml   settings from a file instead (YAML needs solar-bridge[yaml])
    solar-bridge --check              read every device once and show what would be served
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from . import addons
from .bridge import Bridge
from .config import ConfigError
from .web import App

DEFAULT_FILE = Path("/etc/solar-bridge/config.yaml")


def _fmt(value: float | None, unit: str = "W") -> str:
    return "—" if value is None else f"{value:,.0f} {unit}"


async def check(bridge: Bridge) -> int:
    """Read every source twice and show what would be served. Nothing is started."""
    for source in bridge.sources.values():
        await source.start()
    try:
        for _ in range(2):
            await bridge.update()
            await asyncio.sleep(1)
    finally:
        for source in bridge.sources.values():
            await source.stop()

    ok = not bridge.problems
    for problem in bridge.problems:
        print(f"✗ {problem}")
    print("Devices:")
    for name, source in bridge.sources.items():
        st = source.status()
        ok &= st["ok"]
        readings = ", ".join(f"{k}={v:g}" for k, v in st["values"].items())
        detail = f"{readings}  ({st['error']})" if readings and st["error"] else st["error"] or readings
        print(f"  {'✓' if st['ok'] else '✗'} {name} ({st['type']}): {detail}")
    v = bridge.values
    print("\nValues (grid + importing, battery + discharging, load + consuming):")
    for key, unit in (("grid", "W"), ("pv", "W"), ("battery", "W"), ("load", "W"), ("soc", "%")):
        print(f"  {key:8} {_fmt(v.get(key), unit)}")
    if v.get("frequency") is not None:
        print(f"  {'frequency':8} {v['frequency']:.2f} Hz")
    assert bridge.config
    if bridge.config.meters:
        print("\nSmart Meter IPs (+ = from the grid side into the device):")
        for meter in bridge.config.meters:
            power = bridge.meter_data[meter.name].get("P_Grid")
            print(f"  unit {meter.unit_id:3} {meter.name} ({meter.role}): {_fmt(power)}")
    status = bridge.off_grid_status
    if status is not None:
        print(f"\nOff grid — the Wattpilot sees grid {_fmt(status.grid)}: {status.reason}")
    if bridge.data.get("P_Grid") is None:
        print("\n✗ No grid power — the Wattpilot would see P_Grid = null.")
        ok = False
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="solar-bridge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--config", type=Path, help="settings file (YAML or JSON) instead of the web page")
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("STATE_DIRECTORY", "/var/lib/solar-bridge")),
                        help="where settings, energy totals and add-ons are kept")
    parser.add_argument("--port", type=int, default=80, help="web page port before anything is configured")
    parser.add_argument("--check", action="store_true", help="read every device once, show the result, exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config_file = args.config or (Path(os.environ["SOLAR_BRIDGE_CONFIG"]) if os.environ.get("SOLAR_BRIDGE_CONFIG")
                                  else DEFAULT_FILE if DEFAULT_FILE.exists() else None)
    app = App(args.data_dir, config_file, args.port)

    if args.check:
        addons.enable(args.data_dir)
        try:
            config = app.load_config()
        except ConfigError as err:
            sys.exit(f"Configuration problem: {err}")
        if config is None:
            sys.exit("Not configured yet — open the settings page (http://<this device>/settings).")
        sys.exit(asyncio.run(check(Bridge(config, args.data_dir))))

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await app.run(stop)

    try:
        asyncio.run(run())
    except OSError as err:
        sys.exit(f"Cannot start: {err}")


if __name__ == "__main__":
    main()
