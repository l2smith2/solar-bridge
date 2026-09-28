"""Configuration: a dict (from the web page's JSON file, or an optional YAML file) → Config.

See deploy/config.example.yaml for every option.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VALUE_NAMES = ("grid", "pv", "battery", "soc", "load")
METER_ROLES = ("grid", "generator", "load")
SOURCE_TYPES = ("tesla", "http_json", "mqtt", "modbus")
_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class ConfigError(ValueError):
    """Invalid configuration, with a message meant for the user."""


@dataclass
class ValueRef:
    """Where a value comes from: one or more `source.field` readings (summed), negated/scaled."""

    parts: list[tuple[str, str]]
    invert: bool = False
    scale: float = 1.0


@dataclass
class MeterConfig:
    name: str
    unit_id: int
    value: str  # one of VALUE_NAMES (not soc)
    role: str = "grid"
    invert: bool = False


@dataclass
class Config:
    name: str = "fronius-virtual"
    display_name: str | None = None
    http_port: int = 80
    modbus_port: int = 502
    update_interval: float = 5.0
    data_dir: Path | None = None  # None = the directory given on the command line
    wattpilot: bool = True  # serve the Solar API + mDNS for a Wattpilot
    grid_phases: int = 1
    breaker_amps: float = 32.0
    settings_password_hash: str | None = None
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    values: dict[str, ValueRef] = field(default_factory=dict)
    meters: list[MeterConfig] = field(default_factory=list)

    @property
    def serial(self) -> str:
        return self.display_name or self.name


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000)
    return hmac.compare_digest(test.hex(), digest)


def _value_ref(name: str, raw: Any) -> ValueRef:
    spec = raw if isinstance(raw, dict) else {"from": raw}
    refs = spec.get("from")
    refs = [refs] if isinstance(refs, str) else refs
    if not refs or not all(isinstance(r, str) and "." in r for r in refs):
        raise ConfigError(
            f"values.{name}: use 'source.field', a list of them to add up, or {{from: source.field, invert: true}}"
        )
    parts = [tuple(r.split(".", 1)) for r in refs]
    return ValueRef(parts, bool(spec.get("invert", False)), float(spec.get("scale", 1.0)))  # type: ignore[arg-type]


def parse(raw: dict[str, Any]) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("the configuration must be a mapping")
    cfg = Config()
    for key in ("name", "display_name"):
        if raw.get(key):
            setattr(cfg, key, str(raw[key]).strip())
    try:
        for key, cast in (("http_port", int), ("modbus_port", int), ("update_interval", float)):
            if raw.get(key) not in (None, ""):
                setattr(cfg, key, cast(raw[key]))
        grid = raw.get("grid") or {}
        cfg.grid_phases = int(grid.get("phases", 1))
        cfg.breaker_amps = float(grid.get("breaker_amps", 32))
    except (TypeError, ValueError) as err:
        raise ConfigError(f"expected a number: {err}") from err
    if raw.get("data_dir"):
        cfg.data_dir = Path(raw["data_dir"])
    cfg.wattpilot = bool(raw.get("wattpilot", True))
    cfg.settings_password_hash = raw.get("settings_password_hash") or None

    if not _HOSTNAME.match(cfg.name):
        raise ConfigError("name: letters, digits and hyphens only — it is also the network name (name.local)")
    if cfg.grid_phases not in (1, 3):
        raise ConfigError("grid.phases must be 1 or 3")
    if cfg.update_interval < 1:
        raise ConfigError("update_interval must be at least 1 second")

    sources = raw.get("sources") or {}
    if not isinstance(sources, dict) or not sources:
        raise ConfigError("add at least one device to read from")
    for sname, scfg in sources.items():
        if not re.match(r"^[A-Za-z0-9_-]+$", str(sname)):
            raise ConfigError(f"device name '{sname}': letters, digits, - and _ only")
        if not isinstance(scfg, dict) or scfg.get("type") not in SOURCE_TYPES:
            raise ConfigError(f"sources.{sname}.type must be one of {', '.join(SOURCE_TYPES)}")
    cfg.sources = sources

    for vname, vraw in (raw.get("values") or {}).items():
        if vname not in VALUE_NAMES:
            raise ConfigError(f"values.{vname}: unknown value (use {', '.join(VALUE_NAMES)})")
        if vraw in (None, "", []):
            continue
        ref = _value_ref(vname, vraw)
        for source, _ in ref.parts:
            if source not in sources:
                raise ConfigError(f"values.{vname}: no device called '{source}'")
        cfg.values[vname] = ref
    if "grid" not in cfg.values:
        raise ConfigError("grid power is required — the Wattpilot and Fronius need it")

    units: set[int] = set()
    for i, m in enumerate(raw.get("meters") or []):
        try:
            meter = MeterConfig(
                name=str(m.get("name") or f"meter-{i + 1}"),
                unit_id=int(m.get("unit_id", 240 + i)),
                value=str(m.get("value", "grid")),
                role=str(m.get("role", "grid")),
                invert=bool(m.get("invert", False)),
            )
        except (TypeError, ValueError) as err:
            raise ConfigError(f"meters[{i}]: {err}") from err
        if meter.value not in cfg.values or meter.value == "soc":
            raise ConfigError(f"meter '{meter.name}': '{meter.value}' is not a configured power value")
        if meter.role not in METER_ROLES:
            raise ConfigError(f"meter '{meter.name}': role must be one of {', '.join(METER_ROLES)}")
        if meter.unit_id in units or not 1 <= meter.unit_id <= 247:
            raise ConfigError(f"meter '{meter.name}': unit ID must be unique and between 1 and 247")
        units.add(meter.unit_id)
        cfg.meters.append(meter)
    return cfg


def read_file(path: str | Path) -> dict[str, Any]:
    """Raw config from a .json file, or a YAML file (needs PyYAML: solar-bridge[yaml])."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as err:
        raise ConfigError(f"config file not found: {path}") from err
    if path.suffix == ".json":
        try:
            return json.loads(text)
        except ValueError as err:
            raise ConfigError(f"{path} is not valid JSON: {err}") from err
    try:
        import yaml
    except ImportError as err:
        raise ConfigError("YAML config files need PyYAML: pip install 'solar-bridge[yaml]'") from err
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as err:
        raise ConfigError(f"{path} is not valid YAML: {err}") from err


def load(path: str | Path) -> Config:
    return parse(read_file(path))
