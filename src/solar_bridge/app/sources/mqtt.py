"""Subscribe to MQTT topics (Victron, Node-RED, evcc, pypowerwall-server, ...).

    type: mqtt
    host: 192.168.1.10
    port: 1883            # optional
    username / password   # optional
    fields:
      grid: home/grid/power                          # payload is a number
      battery: {topic: pw/aggregates, path: battery.instant_power, scale: 1}
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .base import Source, dig

_LOGGER = logging.getLogger(__name__)


def parse_payload(payload: bytes | str, path: str | None) -> float:
    text = payload.decode() if isinstance(payload, bytes) else payload
    if not path:
        try:
            return float(text)
        except ValueError:
            pass
    return float(dig(json.loads(text), path or ""))


class MqttSource(Source):
    def __init__(self, name: str, config: dict[str, Any]) -> None:
        super().__init__(name, config)
        if not config.get("host"):
            raise ValueError("needs a 'host'")
        fields = config.get("fields") or {}
        # a bare string is the topic; a mapping may add a JSON path and scale
        self._fields = {n: s if isinstance(s, dict) else {"topic": str(s)} for n, s in fields.items()}
        if not self._fields or any("topic" not in s for s in self._fields.values()):
            raise ValueError("needs 'fields', each with a topic")
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def handle(self, topic: str, payload: bytes | str) -> None:
        for name, spec in self._fields.items():
            if spec["topic"] == topic:
                try:
                    self.set(name, parse_payload(payload, spec.get("path")) * float(spec.get("scale", 1)))
                except (ValueError, KeyError, IndexError, TypeError) as err:
                    self.error = f"{topic}: cannot read value ({err})"

    async def _run(self) -> None:
        import aiomqtt  # optional dependency: pip install solar-bridge[app]

        while True:
            try:
                async with aiomqtt.Client(
                    self.config["host"],
                    port=int(self.config.get("port", 1883)),
                    username=self.config.get("username"),
                    password=self.config.get("password"),
                ) as client:
                    for topic in {s["topic"] for s in self._fields.values()}:
                        await client.subscribe(topic)
                    self.error = None
                    async for message in client.messages:
                        self.handle(str(message.topic), message.payload)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 — reconnect on anything
                self.error = f"MQTT: {err}"
                _LOGGER.warning("Source %s: %s — retrying in 10 s", self.name, err)
                await asyncio.sleep(10)
