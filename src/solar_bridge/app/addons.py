"""Optional add-ons (extra Python packages) installable from the web page.

Installed with pip into <data_dir>/addons — the one place the hardened systemd
service may write — and put on sys.path, so no root access or restart is needed.
`pip install solar-bridge[tesla]` or `install.sh --tesla` work too.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


@dataclass
class Addon:
    name: str
    label: str
    requirement: str
    module: str
    note: str = ""
    state: str = "idle"  # idle | installing | failed
    log: list[str] = field(default_factory=list)


ADDONS: dict[str, Addon] = {
    "tesla": Addon(
        "tesla",
        "Tesla Powerwall",
        "pypowerwall>=0.10",
        "pypowerwall",
        "Reads Powerwall 2, + and 3 locally (or via Tesla's cloud). Takes a few minutes on older Pis.",
    ),
}


def addons_dir(data_dir: Path) -> Path:
    return data_dir / "addons"


def enable(data_dir: Path) -> None:
    """Make previously installed add-ons importable (appended: the app's own packages win)."""
    path = str(addons_dir(data_dir))
    if path not in sys.path:
        sys.path.append(path)
    importlib.invalidate_caches()


def installed(name: str) -> bool:
    return importlib.util.find_spec(ADDONS[name].module) is not None


def status() -> dict[str, dict]:
    return {
        a.name: {"label": a.label, "note": a.note, "installed": installed(a.name), "state": a.state,
                 "log": a.log[-15:]}
        for a in ADDONS.values()
    }


async def install(name: str, data_dir: Path) -> bool:
    addon = ADDONS[name]
    if addon.state == "installing":
        return False
    addon.state, addon.log = "installing", []
    target = addons_dir(data_dir)
    target.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HOME": str(data_dir), "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--upgrade",
           "--target", str(target), addon.requirement]
    _LOGGER.info("Installing add-on %s: %s", name, " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
        )
        assert proc.stdout
        async for line in proc.stdout:
            addon.log.append(line.decode(errors="replace").rstrip())
        ok = await proc.wait() == 0
    except OSError as err:
        addon.log.append(str(err))
        ok = False
    enable(data_dir)
    ok = ok and installed(name)
    addon.state = "idle" if ok else "failed"
    _LOGGER.log(logging.INFO if ok else logging.ERROR, "Add-on %s install %s", name, "done" if ok else "failed")
    return ok
