"""Shared fixtures: a fake /proc tree and ready-made snapshots."""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from htop_web import collector
from htop_web.model import normalize_snapshot

PAGE = os.sysconf("SC_PAGE_SIZE")


class FakeProc:
    """Writes just enough of a /proc tree for the collector."""

    def __init__(self, root: Path):
        self.root = root
        (root / "sys" / "kernel").mkdir(parents=True)
        (root / "sys" / "kernel" / "hostname").write_text("testhost\n")
        (root / "sys" / "kernel" / "osrelease").write_text("6.1.0-test\n")
        self.set_stat([[100, 0, 50, 1000, 0, 0, 0, 0, 0, 0]] * 2)
        self.set_meminfo()
        self.set_loadavg(0.5, 0.4, 0.3)
        self.set_uptime(3725.5)

    def set_stat(self, cpus: list[list[int]], running: int = 1) -> None:
        total = [sum(col) for col in zip(*cpus)]
        lines = ["cpu  " + " ".join(map(str, total))]
        lines += [f"cpu{i} " + " ".join(map(str, c)) for i, c in enumerate(cpus)]
        lines += ["intr 1 2 3", "ctxt 12345", "btime 1700000000", f"procs_running {running}",
                  "procs_blocked 0"]
        (self.root / "stat").write_text("\n".join(lines) + "\n")

    def set_meminfo(self, **kb: int) -> None:
        values = {"MemTotal": 16000000, "MemFree": 4000000, "MemAvailable": 9000000,
                  "Buffers": 200000, "Cached": 5000000, "SwapCached": 10000,
                  "SwapTotal": 2000000, "SwapFree": 1500000, "Shmem": 300000,
                  "SReclaimable": 400000}
        values.update(kb)
        text = "".join(f"{k}:{v:>16} kB\n" for k, v in values.items())
        (self.root / "meminfo").write_text(text)

    def set_loadavg(self, one: float, five: float, fifteen: float) -> None:
        (self.root / "loadavg").write_text(f"{one:.2f} {five:.2f} {fifteen:.2f} 2/345 6789\n")

    def set_uptime(self, seconds: float) -> None:
        (self.root / "uptime").write_text(f"{seconds:.2f} 12345.67\n")

    def add(self, pid: int, comm: str, args: list[str] | None = None, *, state: str = "S",
            ppid: int = 1, flags: int = 0, utime: int = 0, stime: int = 0,
            priority: int = 20, nice: int = 0, threads: int = 1, start: int = 1000,
            pages: tuple[int, int, int] = (2560, 512, 128)) -> Path:
        d = self.root / str(pid)
        d.mkdir(exist_ok=True)
        fields = [state, ppid, pid, pid, 0, -1, flags, 0, 0, 0, 0, utime, stime, 0, 0,
                  priority, nice, threads, 0, start, pages[0] * PAGE, pages[1]]
        fields += [0] * 30
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(map(str, fields)) + "\n")
        (d / "statm").write_text(f"{pages[0]} {pages[1]} {pages[2]} 1 0 100 0\n")
        raw = b"".join(a.encode() + b"\0" for a in (args or []))
        (d / "cmdline").write_bytes(raw)
        return d


@pytest.fixture
def fake_proc(tmp_path: Path) -> FakeProc:
    return FakeProc(tmp_path / "proc")


def two_samples(fake: FakeProc, change, dt: float = 2.0) -> dict:
    """Sample, let change() modify the tree, sample again, and build a snapshot."""
    sampler = collector.Sampler(str(fake.root))
    before = sampler.sample()
    change(fake)
    after = sampler.sample()
    before["t"] = after["t"] - dt
    return sampler.snapshot(before, after)


def proc_row(pid: int = 100, user: str = "alice", cpu: float = 1.0, mem: float = 0.5,
             cmd: str = "/usr/bin/python3 app.py", state: str = "S", kthread: int = 0,
             nice: int = 0, res: int = 50 * 2**20, time: int = 1234) -> list:
    return [pid, 1, user, 20 + nice, nice, res * 4, res, res // 4, state, cpu, mem, time, 1,
            kthread, cmd, 9, 16]


def raw_snapshot(host: str = "alpha", procs: list[list] | None = None, ncpu: int = 4) -> dict:
    """A collector-format snapshot (before normalize_snapshot)."""
    if procs is None:
        procs = [proc_row(100 + i, cpu=float(i), mem=float(10 - i), cmd=f"/usr/bin/job{i} --n {i}")
                 for i in range(10)]
        procs.append(proc_row(2, "root", 0.0, 0.0, "kthreadd", kthread=1))
    return {
        "v": 1, "host": host, "kernel": "6.1.0-test", "time": 1700000000.0, "dt": 2.0,
        "ncpu": ncpu, "cpu_fields": collector.CPU_FIELDS,
        "cpu": [10.0, 1.0, 5.0, 0.5, 0.5, 2.0, 0.0, 0.0],
        "cpus": [[i, 10.0 * i, 1.0, 5.0, 0.5, 0.5, 2.0, 0.0, 0.0] for i in range(ncpu)],
        "mem": {"total": 16 * 2**30, "used": 5 * 2**30, "buffers": 2**28, "shared": 2**28,
                "cache": 4 * 2**30, "available": 9 * 2**30, "free": 6 * 2**30},
        "swap": {"total": 2 * 2**30, "used": 2**28, "cache": 2**20},
        "load": [0.5, 0.4, 0.3], "uptime": 90061.0,
        "tasks": {"procs": 10, "threads": 30, "kthreads": 1, "running": 1},
        "proc_fields": collector.PROC_FIELDS, "procs": procs,
    }


@pytest.fixture
def snapshot() -> dict:
    return normalize_snapshot(raw_snapshot())


# ------------------------------------------------------------ running servers

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url: str, auth: str | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url)
    if auth:
        request.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, ""


def start_server(*args: str, env: dict | None = None) -> tuple[subprocess.Popen, str]:
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "htop_web", "--port", str(port), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, **(env or {})})
    return proc, f"http://127.0.0.1:{port}"


def wait_json(base: str, auth: str | None = None, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, body = get(base + "/htop.json", auth)
            if status == 200:
                data = json.loads(body)
                if data["hosts"] and all(h["status"] == "ok" for h in data["hosts"]):
                    return data
        except OSError:
            pass
        time.sleep(0.2)
    raise AssertionError("server did not report its hosts in time")


def stop(proc: subprocess.Popen) -> int:
    proc.send_signal(signal.SIGINT)
    try:
        return proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
