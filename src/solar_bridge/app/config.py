"""Configuration file (YAML) for the standalone bridge. See deploy/config.example.yaml."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

VALUE_NAMES = ("grid", "pv", "battery", "soc", "load")
METER_ROLES = ("grid", "generator", "load")
SOURCE_TYPES = ("tesla", "http_json", "mqtt", "modbus")
_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class ConfigError(ValueError):
    """Invalid configuration, with a message meant for the user."""


@dataclass
class ValueRef:
    """Where a value comes from: `source.field`, optionally negated/scaled."""

    source: str
    field: str
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
    data_dir: Path = Path("/var/lib/solar-bridge")
    wattpilot: bool = True  # serve the Solar API + mDNS for a Wattpilot
    grid_phases: int = 1
    breaker_amps: float = 32.0
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    values: dict[str, ValueRef] = field(default_factory=dict)
    meters: list[MeterConfig] = field(default_factory=list)

    @property
    def serial(self) -> str:
        return self.display_name or self.name


def _value_ref(name: str, raw: Any) -> ValueRef:
    spec = {"from": raw} if isinstance(raw, str) else raw
    if not isinstance(spec, dict) or not isinstance(spec.get("from"), str) or "." not in spec["from"]:
        raise ConfigError(f"values.{name}: use 'source.field' or {{from: source.field, invert: true}}")
    source, _, fld = spec["from"].partition(".")
    return ValueRef(source, fld, bool(spec.get("invert", False)), float(spec.get("scale", 1.0)))


def parse(raw: dict[str, Any]) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("the config file must be a YAML mapping")
    cfg = Config()
    for key in ("name", "display_name"):
        if raw.get(key) is not None:
            setattr(cfg, key, str(raw[key]).strip())
    for key, cast in (("http_port", int), ("modbus_port", int), ("update_interval", float)):
        if key in raw:
            setattr(cfg, key, cast(raw[key]))
    if "data_dir" in raw:
        cfg.data_dir = Path(raw["data_dir"])
    cfg.wattpilot = bool(raw.get("wattpilot", True))
    grid = raw.get("grid") or {}
    cfg.grid_phases = int(grid.get("phases", 1))
    cfg.breaker_amps = float(grid.get("breaker_amps", 32))

    if not _HOSTNAME.match(cfg.name):
        raise ConfigError("name: letters, digits and hyphens only — it is also the mDNS hostname")
    if cfg.grid_phases not in (1, 3):
        raise ConfigError("grid.phases must be 1 or 3")
    if cfg.update_interval < 1:
        raise ConfigError("update_interval must be at least 1 second")

    sources = raw.get("sources") or {}
    if not isinstance(sources, dict) or not sources:
        raise ConfigError("sources: define at least one data source")
    for sname, scfg in sources.items():
        if not isinstance(scfg, dict) or scfg.get("type") not in SOURCE_TYPES:
            raise ConfigError(f"sources.{sname}.type must be one of {', '.join(SOURCE_TYPES)}")
    cfg.sources = sources

    values = raw.get("values") or {}
    for vname, vraw in values.items():
        if vname not in VALUE_NAMES:
            raise ConfigError(f"values.{vname}: unknown value (use {', '.join(VALUE_NAMES)})")
        ref = _value_ref(vname, vraw)
        if ref.source not in sources:
            raise ConfigError(f"values.{vname}: no source called '{ref.source}'")
        cfg.values[vname] = ref
    if "grid" not in cfg.values:
        raise ConfigError("values.grid is required — the Wattpilot and Fronius need grid power")

    units: set[int] = set()
    for i, m in enumerate(raw.get("meters") or []):
        meter = MeterConfig(
            name=str(m.get("name", f"meter-{i + 1}")),
            unit_id=int(m.get("unit_id", 240 + i)),
            value=str(m.get("value", "grid")),
            role=str(m.get("role", "grid")),
            invert=bool(m.get("invert", False)),
        )
        if meter.value not in cfg.values or meter.value == "soc":
            raise ConfigError(f"meters[{i}].value: '{meter.value}' is not a configured power value")
        if meter.role not in METER_ROLES:
            raise ConfigError(f"meters[{i}].role must be one of {', '.join(METER_ROLES)}")
        if meter.unit_id in units or not 1 <= meter.unit_id <= 247:
            raise ConfigError(f"meters[{i}].unit_id must be unique and between 1 and 247")
        units.add(meter.unit_id)
        cfg.meters.append(meter)
    return cfg


def load(path: str | Path) -> Config:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise ConfigError(f"config file not found: {path}") from err
    except yaml.YAMLError as err:
        raise ConfigError(f"config file is not valid YAML: {err}") from err
    return parse(raw)
