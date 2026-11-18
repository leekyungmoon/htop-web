"""The web app: pages, JSON/text endpoints, websocket pushes, and the guards."""

from __future__ import annotations

import asyncio
import base64
import re
import time

import aiohttp
import pytest
from conftest import raw_snapshot

from htop_web.app import CSP, HUB, STATIC, Options, create_app, host_name, is_loopback
from htop_web.model import HostSpec, normalize_snapshot

SPECS = [HostSpec(key="alpha", kind="ssh", host="alpha"), HostSpec(key="beta", kind="ssh", host="beta")]


def basic(user: str, password: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def make_app(**options):
    """An app whose hosts we feed by hand (no workers)."""
    return create_app(SPECS, Options(**options), worker_factory=lambda state: None)


async def next_update(ws, timeout=5.0):
    while True:
        msg = await ws.receive_json(timeout=timeout)
        if msg["type"] == "update":
            return msg


async def drain(ws, quiet=0.3):
    """Read until nothing arrives for a while (updates are sent in 50 ms batches)."""
    while True:
        try:
            await ws.receive_json(timeout=quiet)
        except asyncio.TimeoutError:
            return


async def test_page_and_security_headers(aiohttp_client):
    client = await aiohttp_client(make_app())
    resp = await client.get("/")
    assert resp.status == 200 and resp.content_type == "text/html"
    assert resp.headers["Content-Security-Policy"] == CSP
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    html = await resp.text()
    assert 'src="static/app.js"' in html
    for name in ("app.js", "style.css", "favicon.svg"):
        static = await client.get(f"/static/{name}")
        assert static.status == 200, name
        assert static.headers["Content-Security-Policy"] == CSP


def test_page_loads_nothing_from_elsewhere():
    html = (STATIC / "index.html").read_text()
    for url in re.findall(r'(?:src|href)="([^"]+)"', html):
        assert url.startswith("static/") or url == "https://github.com/leekyungmoon/htop-web", url
    js = (STATIC / "app.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "eval(" not in js
    assert "@import" not in (STATIC / "style.css").read_text()


async def test_json_endpoint(aiohttp_client):
    app = make_app()
    app[HUB].hosts[0].update(normalize_snapshot(raw_snapshot("alpha")))
    client = await aiohttp_client(app)
    data = await (await client.get("/htop.json")).json()
    assert [h["key"] for h in data["hosts"]] == ["alpha", "beta"]
    alpha, beta = data["hosts"]
    assert alpha["status"] == "ok" and alpha["ncpu"] == 4 and len(alpha["procs"]) == 10
    assert beta["status"] == "loading" and "procs" not in beta
    assert data["proc_fields"][0] == "pid" and data["params"]["rows"] == 15

    data = await (await client.get("/htop.json?nodes=alpha&sort=mem&rows=2")).json()
    pid = data["proc_fields"].index("pid")
    assert [h["key"] for h in data["hosts"]] == ["alpha"]
    assert [p[pid] for p in data["hosts"][0]["procs"]] == [100, 101]

    data = await (await client.get("/htop.json?filter=job7&kthreads=1")).json()
    assert data["hosts"][0]["matched"] == 1 and data["hosts"][0]["shown"] == 11


async def test_text_endpoints(aiohttp_client):
    app = make_app(procs=4)
    app[HUB].hosts[0].update(normalize_snapshot(raw_snapshot("alpha")))
    client = await aiohttp_client(app)
    resp = await client.get("/htop.txt?width=100")
    text = await resp.text()
    assert resp.headers["Cache-Control"] == "no-store"
    assert "PID USER" in text and "4 of 10 processes" in text and "beta  loading" in text
    assert "\x1b" not in text
    ansi = await (await client.get("/htop.ansi?nodes=beta")).text()
    assert "alpha" not in ansi and "\x1b[" in ansi
    odd = await (await client.get("/htop.txt?width=abc")).text()
    assert "PID USER" in odd


async def test_hostile_strings_reach_no_one_raw(aiohttp_client):
    evil = "<img src=x onerror=alert(1)>\x1b]0;owned\x07\x1b[2J"
    raw = raw_snapshot("alpha")
    raw["procs"][0][14] = evil
    raw["procs"][0][2] = "<b>root</b>"
    app = make_app()
    app[HUB].hosts[0].update(normalize_snapshot(raw))
    client = await aiohttp_client(app)
    body = await (await client.get("/htop.json?sort=pid&order=asc&rows=50")).text()
    assert "\\u001b" not in body and "\\u0007" not in body
    assert "<img src=x onerror=alert(1)>?]0;owned??[2J" in body
    text = await (await client.get("/htop.txt?sort=pid&order=asc&rows=50&width=200")).text()
    assert "\x1b" not in text and "\x07" not in text


async def test_websocket_pushes(aiohttp_client):
    app = make_app()
    hub = app[HUB]
    hub.hosts[0].update(normalize_snapshot(raw_snapshot("alpha")))
    client = await aiohttp_client(app)
    ws = await client.ws_connect("/ws")
    hello = await ws.receive_json(timeout=5)
    assert hello["type"] == "hello" and hello["hosts"] == ["alpha", "beta"]
    assert hello["proc_fields"][0] == "pid" and hello["interval"] == 2.0
    full = await next_update(ws)
    assert full["full"] and [h["key"] for h in full["hosts"]] == ["alpha", "beta"]
    assert full["hosts"][0]["status"] == "ok" and len(full["hosts"][0]["procs"]) == 10

    # A host that changes is pushed on its own.
    await drain(ws)
    hub.hosts[1].update(normalize_snapshot(raw_snapshot("beta")))
    msg = await next_update(ws)
    assert not msg["full"] and [h["key"] for h in msg["hosts"]] == ["beta"]

    # The page changes how it wants to see things.
    await ws.send_json({"sort": "mem", "order": "desc", "rows": 3})
    msg = await next_update(ws)
    assert msg["full"] and [p[0] for p in msg["hosts"][0]["procs"]] == [100, 101, 102]
    await ws.send_json({"nodes": ["beta"]})
    msg = await next_update(ws)
    assert [h["key"] for h in msg["hosts"]] == ["beta"]
    await ws.send_str("not json")
    await ws.send_json(["not", "a", "dict"])

    # Paused pages get nothing until they resume.
    await ws.send_json({"paused": True})
    await next_update(ws)
    hub.hosts[1].update(normalize_snapshot(raw_snapshot("beta")))
    with pytest.raises(asyncio.TimeoutError):
        await next_update(ws, timeout=0.6)
    await ws.send_json({"paused": False})
    assert (await next_update(ws))["full"]
    await ws.close()


async def test_websocket_nodes_from_query(aiohttp_client):
    client = await aiohttp_client(make_app())
    ws = await client.ws_connect("/ws?nodes=beta")
    assert (await ws.receive_json(timeout=5))["hosts"] == ["beta"]
    await ws.close()


async def test_cross_site_websocket_is_refused(aiohttp_client):
    client = await aiohttp_client(make_app())
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await client.ws_connect("/ws", headers={"Origin": "http://evil.example"})
    assert err.value.status == 403
    origin = f"http://{client.host}:{client.port}"
    ws = await client.ws_connect("/ws", headers={"Origin": origin})
    assert (await ws.receive_json(timeout=5))["type"] == "hello"
    await ws.close()


async def test_foreign_host_names_are_refused_on_localhost(aiohttp_client):
    client = await aiohttp_client(make_app())
    assert (await client.get("/", headers={"Host": "evil.example"})).status == 403
    assert (await client.get("/", headers={"Host": "localhost:48110"})).status == 200
    assert (await client.get("/", headers={"Host": "[::1]:48110"})).status == 200
    open_app = await aiohttp_client(make_app(bind="0.0.0.0"))
    assert (await open_app.get("/", headers={"Host": "dashboard.example"})).status == 200


async def test_basic_auth(aiohttp_client):
    client = await aiohttp_client(make_app(auth=("admin", "s3cret")))
    resp = await client.get("/htop.json")
    assert resp.status == 401 and resp.headers["WWW-Authenticate"].startswith("Basic")
    assert (await client.get("/", headers=basic("admin", "wrong"))).status == 401
    assert (await client.get("/", headers={"Authorization": "Basic !!!"})).status == 401
    assert (await client.get("/", headers=basic("admin", "s3cret"))).status == 200
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await client.ws_connect("/ws")
    assert err.value.status == 401
    ws = await client.ws_connect("/ws", headers=basic("admin", "s3cret"))
    assert (await ws.receive_json(timeout=5))["type"] == "hello"
    await ws.close()


async def test_stale_hosts_are_marked(aiohttp_client):
    app = make_app(interval=1.0)
    hub = app[HUB]
    state = hub.hosts[0]
    state.update(normalize_snapshot(raw_snapshot("alpha")))
    hub.mark_stale(time.time())
    assert state.status == "ok"
    hub.mark_stale(time.time() + hub.stale_after + 1)
    assert state.status == "stale" and state.message
    state.update(normalize_snapshot(raw_snapshot("alpha")))
    assert state.status == "ok"


async def test_shutdown_closes_pages(aiohttp_client):
    client = await aiohttp_client(make_app())
    ws = await client.ws_connect("/ws")
    await ws.receive_json(timeout=5)
    await client.server.close()
    msg = await ws.receive(timeout=5)
    while msg.type == aiohttp.WSMsgType.TEXT:
        msg = await ws.receive(timeout=5)
    assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)


def test_address_helpers():
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("example.com")
    assert host_name("Example.com:80") == "example.com"
    assert host_name("[::1]:8080") == "::1" and host_name("::1") == "::1"
