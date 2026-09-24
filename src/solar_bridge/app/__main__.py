"""Command line: `solar-bridge --config /etc/solar-bridge/config.yaml [--check]`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .bridge import Bridge
from .config import ConfigError, load


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

    ok = True
    print("Sources:")
    for name, source in bridge.sources.items():
        st = source.status()
        ok &= st["ok"]
        detail = st["error"] or ", ".join(f"{k}={v:g}" for k, v in st["values"].items())
        print(f"  {'✓' if st['ok'] else '✗'} {name} ({st['type']}): {detail}")
    v = bridge.values
    print("\nValues (grid + importing, battery + discharging, load + consuming):")
    for key, unit in (("grid", "W"), ("pv", "W"), ("battery", "W"), ("load", "W"), ("soc", "%")):
        print(f"  {key:8} {_fmt(v.get(key), unit)}")
    if bridge.config.meters:
        print("\nModbus meters (+ = from the grid side into the device):")
        for meter in bridge.config.meters:
            power = bridge.meter_data[meter.name].get("P_Grid")
            print(f"  unit {meter.unit_id:3} {meter.name} ({meter.role}): {_fmt(power)}")
    if v.get("grid") is None:
        print("\n✗ No grid power — the Wattpilot would see P_Grid = null.")
        ok = False
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="solar-bridge", description=__doc__)
    parser.add_argument("-c", "--config", default="/etc/solar-bridge/config.yaml")
    parser.add_argument("--check", action="store_true", help="test the config and sources, then exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        bridge = Bridge(load(args.config))
    except (ConfigError, ValueError) as err:
        sys.exit(f"Config error: {err}")

    if args.check:
        sys.exit(asyncio.run(check(bridge)))

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await bridge.run(stop)

    asyncio.run(run())


if __name__ == "__main__":
    main()
