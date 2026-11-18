"""The aiohttp application: the page, JSON/text endpoints and live websocket updates."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import ipaddress
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from aiohttp import WSCloseCode, WSMsgType, web

from . import __version__
from .model import PROC_FIELDS, HostSpec, HostState, ViewParams, host_view, select_hosts
from .render import render_text
from .sources import make_worker

log = logging.getLogger("htop_web")
STATIC = Path(__file__).with_name("static")

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self' ws: wss:; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


@dataclass
class Options:
    interval: float = 2.0
    procs: int = 15
    python: str = "python3"
    verify_host: bool = True
    ssh_options: dict = field(default_factory=dict)  # extra asyncssh.connect() arguments
    auth: tuple[str, str] | None = None
    bind: str = "127.0.0.1"
    min_backoff: float = 1.0
    max_backoff: float = 30.0


class Viewer:
    """One open page."""

    def __init__(self, ws: web.WebSocketResponse, params: ViewParams):
        self.ws = ws
        self.params = params
        self.paused = False
        self.lock = asyncio.Lock()


class Hub:
    """Holds every host's state and pushes changes to the open pages."""

    def __init__(self, options: Options):
        self.options = options
        self.hosts: list[HostState] = []
        self.viewers: set[Viewer] = set()
        self.default_params = ViewParams(rows=options.procs)
        self.stale_after = max(3 * options.interval, options.interval + 5)
        self._dirty: set[str] = set()
        self._wake = asyncio.Event()

    def host_changed(self, state: HostState) -> None:
        self._dirty.add(state.spec.key)
        self._wake.set()

    async def broadcast_loop(self) -> None:
        while True:
            await self._wake.wait()
            await asyncio.sleep(0.05)  # send hosts that update together in one message
            self._wake.clear()
            keys, self._dirty = self._dirty, set()
            results = await asyncio.gather(*(self.send(v, keys) for v in list(self.viewers)),
                                           return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    log.error("could not update a page", exc_info=result)

    def mark_stale(self, now: float) -> None:
        for state in self.hosts:
            if (state.status == "ok" and state.updated is not None
                    and now - state.updated > self.stale_after):
                state.set_status("stale", "No new data from this host")

    async def stale_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self.mark_stale(time.time())

    def views(self, params: ViewParams, keys: Iterable[str] | None = None) -> list[dict]:
        now = time.time()
        hosts = select_hosts(self.hosts, params.nodes)
        if keys is not None:
            wanted = set(keys)
            hosts = [h for h in hosts if h.spec.key in wanted]
        return [host_view(h, params, now) for h in hosts]

    async def send(self, viewer: Viewer, keys: Iterable[str] | None = None,
                   force: bool = False) -> None:
        """Send fresh views to one page; keys=None sends every host it shows."""
        if viewer.paused and not force:
            return
        hosts = self.views(viewer.params, keys)
        if keys is not None and not hosts:
            return
        message = dumps({"type": "update", "full": keys is None, "time": time.time(),
                         "hosts": hosts})
        try:
            async with viewer.lock:
                if not viewer.ws.closed:
                    await asyncio.wait_for(viewer.ws.send_str(message), timeout=10)
        except (asyncio.TimeoutError, ConnectionError, RuntimeError) as exc:
            log.info("dropping a viewer that stopped reading: %s", exc)
            self.viewers.discard(viewer)
            await viewer.ws.close(code=WSCloseCode.GOING_AWAY)


HUB = web.AppKey("hub", Hub)
TASKS = web.AppKey("tasks", list)


# -------------------------------------------------------------------- security

def is_loopback(address: str) -> bool:
    if address == "localhost":
        return True
    try:
        return ipaddress.ip_address(address.strip("[]")).is_loopback
    except ValueError:
        return False


def host_name(host_header: str) -> str:
    """The name part of a Host header ("[::1]:8080" -> "::1")."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


@web.middleware
async def localhost_only(request: web.Request, handler: Callable) -> web.StreamResponse:
    """Refuse other Host names while bound to localhost (stops DNS rebinding)."""
    name = host_name(request.host)
    if name == "localhost" or name.endswith(".localhost") or is_loopback(name):
        return await handler(request)
    raise web.HTTPForbidden(text="htop-web is listening on localhost only; "
                                 "this Host header is not allowed.\n")


def basic_auth(user: str, password: str) -> Callable:
    expected = f"{user}:{password}".encode("utf-8")

    @web.middleware
    async def middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
        scheme, _, value = request.headers.get("Authorization", "").partition(" ")
        given = b""
        if scheme.lower() == "basic":
            try:
                given = base64.b64decode(value.strip(), validate=True)
            except (binascii.Error, ValueError):
                given = b""
        if not hmac.compare_digest(given, expected):
            raise web.HTTPUnauthorized(
                text="Authentication required\n",
                headers={"WWW-Authenticate": 'Basic realm="htop-web", charset="UTF-8"'})
        return await handler(request)

    return middleware


async def security_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")


def same_origin(request: web.Request) -> bool:
    """Browsers send Origin on websockets; refuse pages from other sites."""
    origin = request.headers.get("Origin")
    if origin is None:
        return True  # not a browser (curl, scripts)
    try:
        netloc = urlsplit(origin).netloc
    except ValueError:
        return False
    return netloc.lower() == request.host.lower()


# -------------------------------------------------------------------- handlers

async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


def query_params(request: web.Request) -> ViewParams:
    return request.app[HUB].default_params.updated(request.query)


async def snapshot_json(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    params = query_params(request)
    body = {"version": __version__, "time": time.time(), "interval": hub.options.interval,
            "params": params.as_dict(), "proc_fields": PROC_FIELDS,
            "hosts": hub.views(params)}
    return web.json_response(body, dumps=dumps, headers={"Cache-Control": "no-store"})


def text_endpoint(color: bool) -> Callable:
    async def handler(request: web.Request) -> web.Response:
        hub = request.app[HUB]
        params = query_params(request)
        try:
            width = int(request.query.get("width", "120"))
        except ValueError:
            width = 120
        text = render_text(hub.views(params), params, width=width, color=color)
        return web.Response(text=text, content_type="text/plain", charset="utf-8",
                            headers={"Cache-Control": "no-store"})
    return handler


async def websocket(request: web.Request) -> web.WebSocketResponse:
    hub = request.app[HUB]
    if not same_origin(request):
        raise web.HTTPForbidden(text="Cross-origin websocket refused\n")
    ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=64 * 1024)
    await ws.prepare(request)
    viewer = Viewer(ws, query_params(request))
    hub.viewers.add(viewer)
    try:
        await ws.send_str(dumps({
            "type": "hello", "version": __version__, "interval": hub.options.interval,
            "stale_after": hub.stale_after, "params": viewer.params.as_dict(),
            "proc_fields": PROC_FIELDS,
            "hosts": [h.spec.key for h in select_hosts(hub.hosts, viewer.params.nodes)],
        }))
        await hub.send(viewer, force=True)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            viewer.params = viewer.params.updated(data)
            if "paused" in data:
                viewer.paused = bool(data["paused"])
            await hub.send(viewer, force=True)
    finally:
        hub.viewers.discard(viewer)
    return ws


# ------------------------------------------------------------------------- app

def create_app(specs: list[HostSpec], options: Options | None = None,
               worker_factory: Callable[[HostState], Any] | None = None) -> web.Application:
    """Build the web app. worker_factory(state) may return None to feed a host by hand."""
    options = options or Options()
    hub = Hub(options)
    hub.hosts = [HostState(spec, hub.host_changed) for spec in specs]
    factory = worker_factory or (lambda state: make_worker(state, options))
    workers = [w for w in (factory(state) for state in hub.hosts) if w is not None]

    middlewares = []
    if is_loopback(options.bind):
        middlewares.append(localhost_only)
    if options.auth:
        middlewares.append(basic_auth(*options.auth))
    app = web.Application(middlewares=middlewares)
    app[HUB] = hub
    app.on_response_prepare.append(security_headers)

    async def start(app: web.Application) -> None:
        tasks = [asyncio.create_task(w.run()) for w in workers]
        tasks.append(asyncio.create_task(hub.broadcast_loop()))
        tasks.append(asyncio.create_task(hub.stale_loop()))
        app[TASKS] = tasks

    async def close_pages(app: web.Application) -> None:
        for viewer in list(hub.viewers):
            await viewer.ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutdown")

    async def stop(app: web.Application) -> None:
        tasks = app.get(TASKS, [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app.on_startup.append(start)
    app.on_shutdown.append(close_pages)
    app.on_cleanup.append(stop)

    app.router.add_get("/", index)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/htop.json", snapshot_json)
    app.router.add_get("/htop.txt", text_endpoint(color=False))
    app.router.add_get("/htop.ansi", text_endpoint(color=True))
    app.router.add_static("/static/", STATIC)
    return app
