"""Where snapshots come from: this machine, SSH hosts, or the demo."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import shlex
import socket
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncssh

from .model import HostState, normalize_snapshot

log = logging.getLogger("htop_web")

COLLECTOR_SOURCE = Path(__file__).with_name("collector.py").read_text(encoding="utf-8")
MAX_LINE = 64 * 1024 * 1024  # one snapshot is one line; leave room for huge hosts


class CollectorExited(Exception):
    """The collector process ended (it should run until we disconnect)."""

    def __init__(self, status: int | None, stderr: list[str], python: str):
        super().__init__(status)
        self.status = status
        self.stderr = stderr
        self.python = python

    def __str__(self) -> str:
        last = self.stderr[-1] if self.stderr else ""
        if self.status == 127 or "not found" in last or "No such file" in last:
            return (f"{self.python} was not found on the host. Install Python 3.6+ "
                    f"there, or point --python at it.")
        detail = f": {last}" if last else ""
        if self.status is None:
            return f"The collector stopped{detail}"
        return f"The collector exited with status {self.status}{detail}"


def describe_error(exc: BaseException, state: HostState) -> str:
    """Turn a connection error into one line a person can act on."""
    spec = state.spec
    where = spec.host or spec.key
    if isinstance(exc, CollectorExited):
        return str(exc)
    if isinstance(exc, asyncssh.HostKeyNotVerifiable):
        return (f"Host key is not trusted for {where}. Run 'ssh {spec.key}' once to "
                f"accept it, or pass --no-verify-host.")
    if isinstance(exc, asyncssh.PermissionDenied):
        return f"Permission denied: SSH authentication failed for {spec.key}"
    if isinstance(exc, asyncssh.DisconnectError):
        return f"Disconnected: {exc.reason}"
    if isinstance(exc, asyncssh.ChannelOpenError):
        return f"Could not open an SSH channel: {exc.reason}"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "Timed out"
    if isinstance(exc, socket.gaierror):
        return f"Cannot resolve host name {where}"
    if isinstance(exc, ConnectionRefusedError):
        return f"Connection refused by {where}"
    if isinstance(exc, OSError):
        return exc.strerror or str(exc) or type(exc).__name__
    return f"{type(exc).__name__}: {exc}"


async def _tail(reader: Any, lines: collections.deque) -> None:
    """Keep the last few lines of stderr for error messages."""
    try:
        while True:
            line = await reader.readline()
            if not line:
                return
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            if line.strip():
                lines.append(line.strip()[:300])
    except Exception:  # the stream went away with the process
        return


class Worker:
    """Keep one host's snapshots flowing; reconnect with backoff when it fails."""

    def __init__(self, state: HostState, interval: float, *, min_backoff: float = 1.0,
                 max_backoff: float = 30.0, read_timeout: float | None = None):
        self.state = state
        self.interval = interval
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self.read_timeout = read_timeout or max(15.0, interval * 5)
        self.got_data = False
        self.process: Any = None  # the current collector process, for tests

    def collector_args(self) -> list[str]:
        return ["--interval", f"{self.interval:g}"]

    async def run(self) -> None:
        backoff = self.min_backoff
        while True:
            self.got_data = False
            try:
                await self.session()
                message = "The collector stopped"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # report and retry; one host must not stop the rest
                message = describe_error(exc, self.state)
                log.info("%s: %s", self.state.spec.key, message, exc_info=log.isEnabledFor(10))
            if self.got_data:
                backoff = self.min_backoff
            self.state.set_status("error", f"{message} (retrying in {backoff:g}s)")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    async def session(self) -> None:
        raise NotImplementedError

    def connecting(self, message: str) -> None:
        self.state.set_status("connecting", message)

    async def pump(self, readline: Callable[[], Awaitable[Any]]) -> None:
        """Feed snapshot lines into the host state until EOF."""
        while True:
            try:
                line = await asyncio.wait_for(readline(), timeout=self.read_timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"no data for {self.read_timeout:g}s") from None
            if not line:
                return
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            line = line.strip()
            if not line.startswith("{"):
                continue  # login banners and other shell noise
            try:
                snapshot = normalize_snapshot(json.loads(line))
            except (ValueError, TypeError) as exc:
                log.info("%s: ignoring a bad snapshot: %s", self.state.spec.key, exc)
                continue
            self.got_data = True
            self.state.update(snapshot)


class LocalWorker(Worker):
    """Run the collector on this machine as a child process (no SSH)."""

    async def session(self) -> None:
        self.connecting("Starting the collector ...")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-", *self.collector_args(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=MAX_LINE)
        self.process = proc
        errors: collections.deque = collections.deque(maxlen=5)
        tail = asyncio.create_task(_tail(proc.stderr, errors))
        try:
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(COLLECTOR_SOURCE.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionError):
                pass  # it already ended; stderr and the exit status explain why
            await self.pump(proc.stdout.readline)
            status = await asyncio.wait_for(proc.wait(), timeout=5)
            await asyncio.wait({tail}, timeout=1)
            raise CollectorExited(status, list(errors), sys.executable)
        finally:
            tail.cancel()
            if proc.returncode is None:
                proc.kill()
            try:  # drain the pipes so the transport closes now, not at garbage collection
                await asyncio.wait_for(proc.communicate(), timeout=5)
            except (asyncio.TimeoutError, OSError, ValueError):
                pass


class SSHWorker(Worker):
    """Send the collector over one SSH connection and read its output."""

    def __init__(self, state: HostState, interval: float, *, python: str = "python3",
                 ssh_options: dict | None = None, connect_timeout: float = 15.0, **kwargs: Any):
        super().__init__(state, interval, **kwargs)
        self.python = python
        self.ssh_options = dict(ssh_options or {})
        self.connect_timeout = connect_timeout

    def command(self) -> str:
        words = shlex.split(self.python) + ["-u", "-"] + self.collector_args()
        return " ".join(shlex.quote(w) for w in words)

    def connect_options(self) -> dict:
        spec = self.state.spec
        options = dict(self.ssh_options)
        if spec.port:
            options["port"] = spec.port
        if spec.username:
            options["username"] = spec.username
        options.setdefault("connect_timeout", self.connect_timeout)
        options.setdefault("keepalive_interval", 15)
        options.setdefault("keepalive_count_max", 3)
        # Snapshots are JSON text; compression shrinks them several times over.
        options.setdefault("compression_algs", ["zlib@openssh.com", "none"])
        return options

    async def session(self) -> None:
        spec = self.state.spec
        self.connecting(f"Connecting to {spec.key} ...")
        async with asyncssh.connect(spec.host, **self.connect_options()) as conn:
            self.connecting("Starting the collector ...")
            proc = await conn.create_process(self.command(), encoding="utf-8", errors="replace")
            self.process = proc
            errors: collections.deque = collections.deque(maxlen=5)
            tail = asyncio.create_task(_tail(proc.stderr, errors))
            try:
                try:
                    proc.stdin.write(COLLECTOR_SOURCE)
                    await proc.stdin.drain()
                    proc.stdin.write_eof()
                except (BrokenPipeError, ConnectionError):
                    pass  # the command already ended; its stderr and status explain why
                await self.pump(proc.stdout.readline)
                try:
                    await asyncio.wait_for(proc.wait_closed(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                await asyncio.wait({tail}, timeout=1)
                raise CollectorExited(proc.exit_status, list(errors), self.python)
            finally:
                tail.cancel()
                proc.close()


class DemoWorker:
    """Feed synthetic snapshots for --demo."""

    def __init__(self, state: HostState, interval: float):
        self.state = state
        self.interval = interval

    async def run(self) -> None:
        from .demo import DemoHost

        host = DemoHost(self.state.spec.key)
        while True:
            self.state.update(normalize_snapshot(host.snapshot()))
            await asyncio.sleep(self.interval)


def make_worker(state: HostState, options: Any) -> Any:
    kind = state.spec.kind
    backoff = {"min_backoff": options.min_backoff, "max_backoff": options.max_backoff}
    if kind == "local":
        return LocalWorker(state, options.interval, **backoff)
    if kind == "ssh":
        ssh_options = dict(options.ssh_options)
        if not options.verify_host:
            ssh_options["known_hosts"] = None
        return SSHWorker(state, options.interval, python=options.python,
                         ssh_options=ssh_options, **backoff)
    if kind == "demo":
        return DemoWorker(state, options.interval)
    raise ValueError(f"unknown host kind {kind!r}")
