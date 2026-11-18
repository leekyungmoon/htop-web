"""SSH end to end, against an SSH server this test starts on 127.0.0.1.

The server runs each command on this machine, so the collector really travels
over SSH stdin and its snapshots come back over the channel. Nothing here reads
the real ~/.ssh or talks to another machine.
"""

from __future__ import annotations

import asyncio
import os
import time

import asyncssh
import pytest

from htop_web.app import HUB, Options, create_app
from htop_web.model import HostState, parse_host
from htop_web.sources import LocalWorker

INTERVAL = 0.3


class LocalSSHServer:
    """An SSH server that runs the requested command locally."""

    def __init__(self, host_key, authorized_keys):
        self.host_key = host_key
        self.authorized_keys = authorized_keys
        self.acceptor = None
        self.port = 0
        self.conns: set = set()
        self.children: set = set()

    async def start(self, port: int = 0) -> None:
        server = self

        class Server(asyncssh.SSHServer):
            def connection_made(self, conn):
                self.conn = conn
                server.conns.add(conn)

            def connection_lost(self, exc):
                server.conns.discard(self.conn)

        self.acceptor = await asyncssh.create_server(
            Server, "127.0.0.1", port, server_host_keys=[self.host_key],
            authorized_client_keys=self.authorized_keys, process_factory=self.handle,
            encoding=None)
        self.port = self.acceptor.get_port()

    async def handle(self, process) -> None:
        """Like sshd: copy stdin in, stdout/stderr out, then send the exit status."""
        pipe = asyncio.subprocess.PIPE
        local = await asyncio.create_subprocess_shell(process.command, stdin=pipe, stdout=pipe,
                                                      stderr=pipe)
        self.children.add(local)

        async def feed():
            try:
                while data := await process.stdin.read(65536):
                    local.stdin.write(data)
                    await local.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                local.stdin.close()

        async def copy(src, dst):
            while data := await src.read(65536):
                dst.write(data)

        output = asyncio.ensure_future(asyncio.gather(copy(local.stdout, process.stdout),
                                                      copy(local.stderr, process.stderr)))
        closed = asyncio.ensure_future(process.wait_closed())
        tasks = [asyncio.ensure_future(feed()), output, closed]
        try:
            done, _ = await asyncio.wait({output, closed}, return_when=asyncio.FIRST_COMPLETED)
            if output in done and output.exception() is None:
                process.exit(await local.wait())
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if local.returncode is None:
                local.kill()
            await local.communicate()  # read to EOF so the transport closes cleanly
            self.children.discard(local)

    async def stop(self) -> None:
        self.acceptor.close()
        for conn in list(self.conns):  # before wait_closed(), which waits for them
            conn.close()
        await asyncio.wait_for(self.acceptor.wait_closed(), timeout=10)
        deadline = time.monotonic() + 5
        while self.children and time.monotonic() < deadline:
            await asyncio.sleep(0.05)


@pytest.fixture
async def ssh(tmp_path, monkeypatch):
    """A running server plus a client key and known_hosts that trust it."""
    monkeypatch.setenv("HOME", str(tmp_path))  # keep the real ~/.ssh out of it
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    key_path = tmp_path / "client_key"
    client_key.write_private_key(str(key_path))
    authorized = asyncssh.import_authorized_keys(client_key.export_public_key().decode())
    server = LocalSSHServer(host_key, authorized)
    await server.start()
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(f"[127.0.0.1]:{server.port} {host_key.export_public_key().decode()}")
    server.key_path = key_path
    server.known_hosts = known_hosts
    server.host_key_obj = host_key
    yield server
    await server.stop()


def ssh_options(server, **extra) -> dict:
    options = {"known_hosts": str(server.known_hosts), "client_keys": [str(server.key_path)],
               "agent_path": None}
    options.update(extra)
    return options


async def wait_for(predicate, timeout=20.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError("timed out")


def make_app(spec_text, server, python="python3", **ssh_extra):
    spec = parse_host(spec_text)
    options = Options(interval=INTERVAL, python=python, min_backoff=0.1, max_backoff=0.5,
                      ssh_options=ssh_options(server, **ssh_extra) if server else {})
    return create_app([spec], options)


async def test_collects_over_ssh_and_streams_to_pages(ssh, aiohttp_client):
    app = make_app(f"tester@127.0.0.1:{ssh.port}", ssh)
    client = await aiohttp_client(app)
    ws = await client.ws_connect("/ws")
    assert (await ws.receive_json(timeout=5))["type"] == "hello"
    deadline = time.monotonic() + 20
    view = None
    while time.monotonic() < deadline:
        msg = await ws.receive_json(timeout=20)
        hosts = [h for h in msg.get("hosts", []) if h.get("status") == "ok"]
        if hosts:
            view = hosts[0]
            break
    assert view is not None, "no snapshot arrived over SSH"
    assert view["kind"] == "ssh" and view["ncpu"] >= 1 and view["procs"]
    assert view["mem"]["total"] > 0
    # The collector itself shows up, started through the SSH session.
    cmd = app[HUB].hosts[0].snapshot["procs"]
    assert any("-u - --interval 0.3" in p[14] for p in cmd)
    await ws.close()


async def test_reconnects_after_the_server_goes_away(ssh, aiohttp_client):
    app = make_app(f"tester@127.0.0.1:{ssh.port}", ssh)
    await aiohttp_client(app)
    state = app[HUB].hosts[0]
    await wait_for(lambda: state.status == "ok")
    port = ssh.port
    await ssh.stop()
    await wait_for(lambda: state.status == "error")
    assert "retrying" in state.message
    seq = state.seq
    await ssh.start(port)
    await wait_for(lambda: state.status == "ok" and state.seq > seq + 1)


async def test_follows_ssh_config_aliases(ssh, aiohttp_client, tmp_path):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "config").write_text(
        "Host lab-box\n"
        "    HostName 127.0.0.1\n"
        f"    Port {ssh.port}\n"
        "    User tester\n"
        f"    IdentityFile {ssh.key_path}\n"
        f"    UserKnownHostsFile {ssh.known_hosts}\n")
    spec = parse_host("lab-box")
    assert (spec.port, spec.username) == (None, None)
    app = create_app([spec], Options(interval=INTERVAL, min_backoff=0.1, max_backoff=0.5,
                                     ssh_options={"agent_path": None}))
    await aiohttp_client(app)
    state = app[HUB].hosts[0]
    await wait_for(lambda: state.status == "ok")


async def test_missing_python_is_explained(ssh, aiohttp_client):
    app = make_app(f"tester@127.0.0.1:{ssh.port}", ssh, python="/nonexistent/python3")
    await aiohttp_client(app)
    state = app[HUB].hosts[0]
    await wait_for(lambda: state.status == "error")
    assert "/nonexistent/python3 was not found on the host" in state.message


async def test_bad_key_is_explained(ssh, aiohttp_client, tmp_path):
    other = tmp_path / "other_key"
    asyncssh.generate_private_key("ssh-ed25519").write_private_key(str(other))
    app = make_app(f"tester@127.0.0.1:{ssh.port}", ssh, client_keys=[str(other)])
    await aiohttp_client(app)
    state = app[HUB].hosts[0]
    await wait_for(lambda: state.status == "error")
    assert state.message.startswith("Permission denied")


async def test_untrusted_host_key_is_explained(ssh, aiohttp_client, tmp_path):
    wrong = tmp_path / "wrong_known_hosts"
    impostor = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()
    wrong.write_text(f"[127.0.0.1]:{ssh.port} {impostor}")
    app = make_app(f"tester@127.0.0.1:{ssh.port}", ssh, known_hosts=str(wrong))
    await aiohttp_client(app)
    state = app[HUB].hosts[0]
    await wait_for(lambda: state.status == "error")
    assert state.message.startswith("Host key is not trusted")


async def test_unreachable_host_is_explained(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    app = create_app([parse_host("tester@127.0.0.1:1")],
                     Options(interval=INTERVAL, min_backoff=0.1, max_backoff=0.5,
                             ssh_options={"agent_path": None, "known_hosts": None}))
    await aiohttp_client(app)
    state = app[HUB].hosts[0]
    await wait_for(lambda: state.status == "error")
    assert "refused" in state.message.lower()


async def test_local_worker_runs_and_cleans_up():
    state = HostState(parse_host("local"))
    worker = LocalWorker(state, INTERVAL, min_backoff=0.1)
    task = asyncio.create_task(worker.run())
    try:
        await wait_for(lambda: state.status == "ok")
        assert state.snapshot["procs"] and state.snapshot["host"]
        pid = worker.process.pid
        assert os.path.exists(f"/proc/{pid}")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert worker.process.returncode is not None
