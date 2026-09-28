"""Web page, settings API and the supervisor that (re)starts the bridge.

Settings live in <data_dir>/config.json, written by the settings page. A config
file given on the command line (YAML or JSON) takes precedence; the page then
shows the settings read-only.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from . import addons
from .bridge import Bridge
from .config import Config, ConfigError, check_password, hash_password, parse, read_file
from .sources import create_source
from .templates import TEMPLATES

_LOGGER = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
SECRET_KEYS = ("password", "gw_pwd", "token")
MASK = "••••••••"


def mask_secrets(raw: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(raw)
    out.pop("settings_password_hash", None)
    for scfg in (out.get("sources") or {}).values():
        for key in SECRET_KEYS:
            if scfg.get(key):
                scfg[key] = MASK
    return out


def restore_secrets(new: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """Masked secrets in a submitted config keep their stored value."""
    for name, scfg in (new.get("sources") or {}).items():
        before = (old.get("sources") or {}).get(name) or {}
        for key in SECRET_KEYS:
            if scfg.get(key) == MASK:
                if before.get(key):
                    scfg[key] = before[key]
                else:
                    scfg.pop(key)
    return new


class App:
    def __init__(self, data_dir: Path, config_file: Path | None = None, setup_port: int = 80) -> None:
        self.data_dir = data_dir
        self.config_file = config_file
        self.setup_port = setup_port
        self.store = data_dir / "config.json"
        self.bridge: Bridge | None = None
        self._lock = asyncio.Lock()
        self._accepted_auth: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    # ── configuration ─────────────────────────────────────────────────────

    @property
    def managed_by_file(self) -> bool:
        return self.config_file is not None

    def raw_config(self) -> dict[str, Any]:
        path = self.config_file or self.store
        if not path.exists():
            return {}
        return read_file(path)

    def load_config(self) -> Config | None:
        raw = self.raw_config()
        return parse(raw) if raw else None

    def save_raw(self, raw: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.store.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2))
        os.chmod(tmp, 0o600)  # holds device passwords
        os.replace(tmp, self.store)

    # ── lifecycle ─────────────────────────────────────────────────────────

    def _bridge(self, config: Config | None) -> Bridge:
        return Bridge(config, self.data_dir, self.setup_port, self.add_routes)

    async def _start(self, config: Config | None) -> None:
        self.bridge = self._bridge(config)
        try:
            await self.bridge.start()
        except OSError as err:
            if config is None:
                raise
            problem = f"Could not open port {config.http_port}: {err}. Running on port {self.setup_port} instead."
            _LOGGER.error(problem)
            await self.bridge.stop()
            self.bridge = self._bridge(None)
            self.bridge.problems.append(problem)
            await self.bridge.start()

    async def start(self) -> None:
        addons.enable(self.data_dir)
        try:
            config = self.load_config()
        except ConfigError as err:
            _LOGGER.error("Configuration problem: %s", err)
            config = None
            await self._start(None)
            assert self.bridge
            self.bridge.problems.append(f"Configuration problem: {err}")
            return
        await self._start(config)

    async def reload(self) -> None:
        await asyncio.sleep(0.3)  # let the HTTP response that asked for it go out first
        async with self._lock:
            if self.bridge:
                await self.bridge.stop()
            try:
                config = self.load_config()
            except ConfigError as err:
                await self._start(None)
                assert self.bridge
                self.bridge.problems.append(f"Configuration problem: {err}")
                return
            await self._start(config)

    async def run(self, stop: asyncio.Event) -> None:
        await self.start()
        try:
            while not stop.is_set():
                assert self.bridge
                try:
                    await asyncio.wait_for(stop.wait(), self.bridge.interval)
                except TimeoutError:
                    async with self._lock:
                        await self.bridge.update()
        finally:
            async with self._lock:
                if self.bridge:
                    await self.bridge.stop()

    def _background(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── routes ────────────────────────────────────────────────────────────

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/", self._page("index.html"))
        app.router.add_get("/settings", self._page("settings.html"))
        app.router.add_get("/api/state", self.handle_state)
        app.router.add_get("/api/templates", self.handle_templates)
        app.router.add_get("/api/addons", self.handle_addons)
        app.router.add_post("/api/addons/{name}/install", self.handle_addon_install)
        app.router.add_get("/api/config", self.handle_get_config)
        app.router.add_post("/api/config", self.handle_save_config)
        app.router.add_post("/api/test-source", self.handle_test_source)

    @staticmethod
    def _page(name: str):
        async def handler(request: web.Request) -> web.StreamResponse:
            return web.FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})

        return handler

    def _authorized(self, request: web.Request) -> bool:
        try:
            stored = self.raw_config().get("settings_password_hash")
        except ConfigError:
            stored = None
        if not stored:
            return True
        header = request.headers.get("Authorization", "")
        if header in self._accepted_auth:  # pbkdf2 is slow on small Pis — check each login once
            return True
        if header.startswith("Basic "):
            try:
                _, _, password = base64.b64decode(header[6:]).decode().partition(":")
            except ValueError:
                return False
            if check_password(password, stored):
                self._accepted_auth.add(header)
                return True
        return False

    @staticmethod
    def _unauthorized() -> web.Response:
        # No WWW-Authenticate header: the settings page asks for the password itself
        return web.json_response({"error": "settings password required"}, status=401)

    async def handle_state(self, request: web.Request) -> web.Response:
        assert self.bridge
        return web.json_response(self.bridge.state())

    async def handle_templates(self, request: web.Request) -> web.Response:
        return web.json_response(TEMPLATES)

    async def handle_addons(self, request: web.Request) -> web.Response:
        return web.json_response(addons.status())

    async def handle_addon_install(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        name = request.match_info["name"]
        if name not in addons.ADDONS:
            return web.json_response({"error": "unknown add-on"}, status=404)

        async def install() -> None:
            if await addons.install(name, self.data_dir):
                await self.reload()  # sources that needed it start working

        self._background(install())
        return web.json_response({"ok": True})

    async def handle_get_config(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        try:
            raw = self.raw_config()
        except ConfigError as err:
            raw, error = {}, str(err)
        else:
            error = None
        return web.json_response({
            "config": mask_secrets(raw),
            "managed_by_file": str(self.config_file) if self.config_file else None,
            "has_settings_password": bool(raw.get("settings_password_hash")),
            "error": error,
        })

    async def handle_save_config(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        if self.managed_by_file:
            return web.json_response(
                {"error": f"Settings come from {self.config_file} — edit that file instead."}, status=409
            )
        try:
            new = await request.json()
            assert isinstance(new, dict)
        except (ValueError, AssertionError):
            return web.json_response({"error": "expected a JSON object"}, status=400)
        try:
            old = self.raw_config()
        except ConfigError:
            old = {}
        new = restore_secrets(new, old)
        password = new.pop("settings_password", None)
        if new.pop("remove_settings_password", False):
            new.pop("settings_password_hash", None)
        elif password:
            new["settings_password_hash"] = hash_password(password)
            self._accepted_auth.clear()
        elif old.get("settings_password_hash"):
            new["settings_password_hash"] = old["settings_password_hash"]
        try:
            parse(new)
        except ConfigError as err:
            return web.json_response({"error": str(err)}, status=400)
        self.save_raw(new)
        self._background(self.reload())
        return web.json_response({"ok": True})

    async def handle_test_source(self, request: web.Request) -> web.Response:
        """Read a device once with the given settings, without saving anything."""
        if not self._authorized(request):
            return self._unauthorized()
        try:
            body = await request.json()
            name, scfg = str(body["name"] or "test"), dict(body["config"])
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "expected {name, config}"}, status=400)
        try:
            old = self.raw_config()
        except ConfigError:
            old = {}
        scfg = restore_secrets({"sources": {name: scfg}}, old)["sources"][name]
        try:
            source = create_source(name, scfg)
        except (ValueError, KeyError) as err:
            return web.json_response({"ok": False, "error": str(err), "values": {}})
        try:
            await source.start()
            await asyncio.wait_for(source.refresh(), 45)
            if scfg.get("type") == "mqtt" and not source.status()["values"]:
                for _ in range(20):  # MQTT values arrive when the device next publishes
                    await asyncio.sleep(0.5)
                    if source.status()["values"]:
                        break
        except TimeoutError:
            source.error = "no answer within 45 s"
        finally:
            await source.stop()
        status = source.status()
        return web.json_response({"ok": status["ok"], "error": status["error"], "values": status["values"]})
