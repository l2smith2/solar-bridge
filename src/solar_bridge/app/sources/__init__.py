"""Data sources: where readings come from."""
from __future__ import annotations

from typing import Any

from .base import Source
from .http_json import HttpJsonSource
from .modbus import ModbusSource
from .mqtt import MqttSource
from .sma_speedwire import SmaSpeedwireSource
from .tesla import TeslaSource

SOURCE_TYPES: dict[str, type[Source]] = {
    "tesla": TeslaSource,
    "http_json": HttpJsonSource,
    "mqtt": MqttSource,
    "modbus": ModbusSource,
    "sma_speedwire": SmaSpeedwireSource,
}


def create_source(name: str, config: dict[str, Any]) -> Source:
    try:
        return SOURCE_TYPES[config["type"]](name, config)
    except ValueError as err:
        raise ValueError(f"sources.{name}: {err}") from err


__all__ = ["SOURCE_TYPES", "Source", "create_source"]
