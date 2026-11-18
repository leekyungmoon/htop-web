"""The collector against this machine's real /proc."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from htop_web import collector

pytestmark = pytest.mark.skipif(not Path("/proc/stat").exists(), reason="needs Linux /proc")


def test_matches_proc():
    done = subprocess.run([sys.executable, collector.__file__, "--once", "--interval", "0.3"],
                          capture_output=True, text=True, timeout=30)
    snap = json.loads(done.stdout)
    stat_cpus = [line for line in Path("/proc/stat").read_text().splitlines()
                 if line.startswith("cpu") and line[3].isdigit()]
    assert snap["ncpu"] == len(stat_cpus)
    meminfo = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    kb = {k: int(v.split()[0]) * 1024 for k, v in meminfo.items()}
    assert snap["mem"]["total"] == kb["MemTotal"]
    assert snap["swap"]["total"] == kb["SwapTotal"]
    pids = [p for p in os.listdir("/proc") if p.isdigit()]
    tasks = snap["tasks"]
    assert abs(tasks["procs"] + tasks["kthreads"] - len(pids)) <= max(20, len(pids) // 10)
    load = float(Path("/proc/loadavg").read_text().split()[0])
    assert abs(snap["load"][0] - load) < 1.0
    me = [row for row in snap["procs"] if row[0] == os.getpid()]
    assert me and me[0][2]  # we see ourselves, with a user name


def run_tool(*argv: str) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=30,
                          env={**os.environ, "LC_ALL": "C", "COLUMNS": "200"}).stdout


@pytest.mark.skipif(not shutil.which("free") or not shutil.which("top"), reason="needs procps")
def test_agrees_with_free_and_top():
    done = subprocess.run([sys.executable, collector.__file__, "--once", "--interval", "0.3"],
                          capture_output=True, text=True, timeout=30)
    snap = json.loads(done.stdout)
    free = run_tool("free", "-b").splitlines()
    total, _used, _free, shared, buff_cache, available = map(int, free[1].split()[1:7])
    swap_total, swap_used = map(int, free[2].split()[1:3])
    top = run_tool("top", "-b", "-n", "1")
    tasks_total = int(re.search(r"Tasks:\s*(\d+)", top).group(1))
    load1 = float(re.search(r"load average:\s*([\d.]+)", top).group(1))

    mem, swap, tasks = snap["mem"], snap["swap"], snap["tasks"]
    # The two readings are about a second apart; on a busy machine the page cache and
    # free memory move in that time, so allow 3% of RAM (at least 64 MiB).
    slack = max(64 * 2**20, total * 3 // 100)
    assert mem["total"] == total and swap["total"] == swap_total
    assert abs(mem["shared"] - shared) < slack
    assert abs(mem["buffers"] + mem["shared"] + mem["cache"] - buff_cache) < slack
    assert abs(mem["available"] - available) < 2 * slack
    assert abs(swap["used"] + swap["cache"] - swap_used) < slack
    assert abs(tasks["procs"] + tasks["kthreads"] - tasks_total) <= max(20, tasks_total // 10)
    assert abs(snap["load"][0] - load1) < 1.0
    assert snap["ncpu"] == int(run_tool("nproc"))
