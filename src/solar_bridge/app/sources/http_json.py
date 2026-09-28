"""Poll any device with a JSON HTTP API (Shelly, Enphase Envoy, OpenDTU, ...).

    type: http_json
    url: http://192.168.1.60/rpc/EM.GetStatus?id=0
    username / password: optional basic auth
    token: optional bearer token (e.g. Enphase Envoy)
    fields:
      grid: total_act_power            # dotted path into the JSON
      pv: {path: inverters.0.power, scale: 1000}
"""
from __future__ import annotations

from typing import Any

import aiohttp

from .base import Source, dig, field_specs


class HttpJsonSource(Source):
    def __init__(self, name: str, config: dict[str, Any]) -> None:
        super().__init__(name, config)
        if not config.get("url"):
            raise ValueError("needs a 'url'")
        self.fields = field_specs(config)
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        auth = None
        if self.config.get("username"):
            auth = aiohttp.BasicAuth(self.config["username"], self.config.get("password", ""))
        headers = dict(self.config.get("headers") or {})
        if self.config.get("token"):
            headers["Authorization"] = f"Bearer {self.config['token']}"
        self._session = aiohttp.ClientSession(
            auth=auth,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=float(self.config.get("timeout", 5))),
        )

    async def stop(self) -> None:
        if self._session:
            await self._session.close()

    async def poll(self) -> None:
        assert self._session
        async with self._session.get(self.config["url"], ssl=bool(self.config.get("verify_ssl", False))) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)

        def read(name: str, spec: dict[str, Any]) -> float | None:
            value = dig(data, spec["path"])
            return None if value is None else float(value) * float(spec.get("scale", 1))

        self.read_fields(read)
