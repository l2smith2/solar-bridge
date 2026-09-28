"""Data source base class."""
from __future__ import annotations

import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)


class Source:
    """Provides named numeric readings, e.g. {"grid": 1250.0, "soc": 81.0}.

    Polling sources implement poll(); push sources (MQTT) call self.set() and
    leave poll() alone. Readings older than `stale_after` seconds count as
    missing, so a dead source serves null rather than a frozen value.
    """

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.config = config
        self.stale_after = float(config.get("stale_after", 120))
        self.fields: dict[str, dict[str, Any]] = {}
        self.error: str | None = None
        self._values: dict[str, tuple[float, float]] = {}

    async def start(self) -> None:
        """Connect / start background work."""

    async def stop(self) -> None:
        """Disconnect."""

    async def poll(self) -> None:
        """Fetch fresh readings (polling sources)."""

    async def refresh(self) -> None:
        try:
            await self.poll()
        except Exception as err:  # noqa: BLE001 — any failure just marks the source unhealthy
            if self.error != str(err):
                _LOGGER.warning("Source %s: %s", self.name, err)
            self.error = str(err) or type(err).__name__

    def read_fields(self, read) -> None:
        """Call read(name, spec) for each field; one failing field doesn't stop the others.

        read() returns a number, or None when the device reports no value (JSON null).
        """
        errors = []
        for name, spec in self.fields.items():
            try:
                value = read(name, spec)
            except Exception as err:  # noqa: BLE001
                errors.append(f"{name}: {err or type(err).__name__}")
                continue
            if value is not None:
                self.set(name, value)
        if errors and len(errors) == len(self.fields):
            raise ConnectionError("; ".join(errors))
        self.error = "; ".join(errors) or None

    def set(self, field: str, value: float) -> None:
        self._values[field] = (float(value), time.monotonic())
        self.error = None

    def get(self, field: str) -> float | None:
        entry = self._values.get(field)
        if entry is None or time.monotonic() - entry[1] > self.stale_after:
            return None
        return entry[0]

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        ages = [now - t for _, t in self._values.values()]
        return {
            "type": self.config.get("type"),
            "ok": self.error is None and bool(ages) and min(ages) <= self.stale_after,
            "error": self.error,
            "age_s": round(min(ages), 1) if ages else None,
            "values": {k: v for k, (v, _) in self._values.items()},
        }


def number(config: dict[str, Any], key: str, default: float, kind: type = int) -> Any:
    """A numeric setting, with an error message people can act on."""
    value = config.get(key, default)
    try:
        return kind(default if value is None else value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number (got '{value}')") from None


def dig(data: Any, path: str) -> Any:
    """Follow a dotted path through nested dicts/lists: 'emeters.0.power'."""
    for part in filter(None, path.split(".")):
        if isinstance(data, list):
            data = data[int(part)]
        elif isinstance(data, dict):
            data = data[part]
        else:
            raise KeyError(path)
    return data


def field_specs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`fields:` entries as dicts; a bare string is shorthand for {path: ...}."""
    fields = config.get("fields") or {}
    if not isinstance(fields, dict) or not fields:
        raise ValueError("needs a 'fields' mapping")
    return {name: spec if isinstance(spec, dict) else {"path": str(spec)} for name, spec in fields.items()}
