"""The collector: /proc parsing and the numbers htop shows."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import PAGE, two_samples

from htop_web import collector

COLLECTOR = Path(collector.__file__)


def by_pid(snap: dict) -> dict[int, dict]:
    fields = snap["proc_fields"]
    return {row[0]: dict(zip(fields, row)) for row in snap["procs"]}


def test_cpu_percentages_split_like_htop():
    before = [100, 10, 50, 1000, 20, 5, 5, 0, 0, 0]
    after = [200, 20, 100, 1800, 40, 10, 10, 10, 0, 0]
    # deltas: user 100 nice 10 system 50 idle 800 iowait 20 irq 5 softirq 5 steal 10
    assert collector.cpu_percentages(before, after) == [10.0, 1.0, 5.0, 0.5, 0.5, 2.0, 1.0, 0.0]


def test_guest_time_is_not_counted_twice():
    before = [0] * 10
    after = [300, 0, 0, 700, 0, 0, 0, 0, 100, 0]  # user includes 100 of guest time
    user, nice, system, irq, softirq, iowait, steal, guest = collector.cpu_percentages(before, after)
    assert (user, guest) == (20.0, 10.0)


def test_cpu_percentages_survive_counters_going_backwards():
    assert collector.cpu_percentages([10] * 10, [5] * 10) == [0.0] * 8
    assert collector.cpu_percentages(None, [1] * 10) == [0.0] * 8


def test_snapshot_numbers(fake_proc):
    fake_proc.add(100, "python3", ["/usr/bin/python3", "train.py"], utime=100, stime=50,
                  threads=4, pages=(25600, 5120, 1280))
    fake_proc.add(200, "sshd", ["sshd: alice@pts/0"], nice=5, priority=25)
    fake_proc.add(2, "kthreadd", flags=collector.PF_KTHREAD, ppid=0)
    fake_proc.add(9, "kworker/0:1-events", flags=collector.PF_KTHREAD, ppid=2)
    clk = os.sysconf("SC_CLK_TCK")

    def change(fake):
        fake.add(100, "python3", ["/usr/bin/python3", "train.py"], utime=100 + clk, stime=50,
                 threads=4, pages=(25600, 5120, 1280), state="R")
        fake.set_stat([[300, 0, 150, 1100, 0, 0, 0, 0, 0, 0],
                       [150, 0, 50, 1300, 0, 0, 0, 0, 0, 0]], running=3)

    snap = two_samples(fake_proc, change, dt=2.0)
    procs = by_pid(snap)

    # One second of CPU over two seconds of wall time.
    assert procs[100]["cpu"] == 50.0
    assert procs[100]["state"] == "R"
    assert procs[100]["res"] == 5120 * PAGE
    assert procs[100]["virt"] == 25600 * PAGE
    assert procs[100]["shr"] == 1280 * PAGE
    assert procs[100]["mem"] == round(5120 * PAGE * 100 / (16000000 * 1024), 1)
    assert procs[100]["time"] == (100 + clk + 50) * 100 // clk
    assert procs[100]["cmd"] == "/usr/bin/python3 train.py"
    assert procs[100]["cmd"][procs[100]["hs"]:procs[100]["he"]] == "python3"

    # A rewritten title highlights the program name only.
    assert procs[200]["cmd"][procs[200]["hs"]:procs[200]["he"]] == "sshd"
    assert (procs[200]["ni"], procs[200]["pri"]) == (5, 25)

    # Kernel threads show their name and are counted apart.
    assert procs[9]["kthread"] == 1 and procs[9]["cmd"] == "kworker/0:1-events"
    assert snap["tasks"] == {"procs": 2, "threads": 3, "kthreads": 2, "running": 2}

    assert snap["ncpu"] == 2
    assert snap["cpus"][0][0] == 0 and snap["cpus"][1][0] == 1
    assert snap["host"] == "testhost" and snap["kernel"] == "6.1.0-test"
    assert snap["load"] == [0.5, 0.4, 0.3]
    assert snap["uptime"] == 3725.5


def test_memory_like_htop(fake_proc):
    snap = two_samples(fake_proc, lambda fake: None)
    k = 1024
    mem, swap = snap["mem"], snap["swap"]
    assert mem["total"] == 16000000 * k
    # used = total - free - buffers - cached - sreclaimable; Cached already holds Shmem
    assert mem["used"] == (16000000 - 4000000 - 200000 - 5000000 - 400000) * k
    assert mem["shared"] == 300000 * k
    assert mem["cache"] == (5000000 + 400000 - 300000) * k
    # The four bar parts add up to total - free.
    assert mem["used"] + mem["buffers"] + mem["shared"] + mem["cache"] == (16000000 - 4000000) * k
    assert swap == {"total": 2000000 * k, "used": (2000000 - 1500000 - 10000) * k,
                    "cache": 10000 * k}


def test_new_process_counts_all_its_ticks(fake_proc):
    clk = os.sysconf("SC_CLK_TCK")
    snap = two_samples(fake_proc, lambda f: f.add(300, "make", ["make"], utime=clk // 2))
    assert by_pid(snap)[300]["cpu"] == 25.0


def test_pid_reuse_is_a_new_process(fake_proc):
    clk = os.sysconf("SC_CLK_TCK")
    fake_proc.add(400, "old", ["old"], utime=50 * clk, start=10)
    snap = two_samples(fake_proc, lambda f: f.add(400, "new", ["new"], utime=clk, start=999))
    row = by_pid(snap)[400]
    assert row["cmd"] == "new" and row["cpu"] == 50.0


def test_odd_command_names_parse(fake_proc):
    fake_proc.add(500, "we(ird) )name", [])
    row = by_pid(two_samples(fake_proc, lambda f: None))[500]
    assert row["cmd"] == "we(ird) )name"  # zombies and kernel threads fall back to comm


def test_vanished_and_unreadable_processes_are_skipped(fake_proc):
    fake_proc.add(600, "ok", ["ok"])
    (fake_proc.root / "601").mkdir()  # exited between listing and reading
    gone = fake_proc.add(602, "secret", ["secret"])
    (gone / "stat").chmod(0)
    try:
        procs = by_pid(two_samples(fake_proc, lambda f: None))
    finally:
        (gone / "stat").chmod(0o644)
    assert 600 in procs and 601 not in procs
    if os.geteuid() != 0:
        assert 602 not in procs


def test_unknown_uid_shows_the_number(fake_proc, monkeypatch):
    def missing(uid):
        raise KeyError(uid)

    monkeypatch.setattr(collector.pwd, "getpwuid", missing)
    fake_proc.add(700, "x", ["x"])
    assert by_pid(two_samples(fake_proc, lambda f: None))[700]["user"] == str(os.getuid())


def test_long_command_lines_are_cut(fake_proc):
    fake_proc.add(800, "java", ["java", "-cp", "x" * 10000])
    assert len(by_pid(two_samples(fake_proc, lambda f: None))[800]["cmd"]) == collector.MAX_CMD


def test_basename_span():
    assert collector.basename_span("/usr/bin/python3 x.py", "/usr/bin/python3") == (9, 16)
    assert collector.basename_span("nginx: worker process", "nginx: worker process") == (0, 5)
    assert collector.basename_span("-zsh", "-zsh") == (0, 4)
    assert collector.basename_span("/", "/") == (0, 0)


def run_collector(*args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], input=stdin, capture_output=True, timeout=30)


def test_runs_as_a_script(fake_proc):
    fake_proc.add(100, "python3", ["/usr/bin/python3"])
    done = run_collector(str(COLLECTOR), "--proc", str(fake_proc.root), "--once",
                         "--interval", "0.2")
    assert done.returncode == 0, done.stderr
    snap = json.loads(done.stdout)
    assert snap["v"] == 1 and snap["host"] == "testhost" and len(snap["procs"]) == 1


def test_runs_from_stdin_like_over_ssh(fake_proc):
    done = run_collector("-u", "-", "--proc", str(fake_proc.root), "--once", "--interval", "0.2",
                         stdin=COLLECTOR.read_bytes())
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.splitlines()[0])["v"] == 1


def test_stops_quietly_when_the_reader_goes_away():
    proc = subprocess.Popen([sys.executable, str(COLLECTOR), "--interval", "0.2"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert json.loads(proc.stdout.readline())["v"] == 1
        proc.stdout.close()
        assert proc.wait(timeout=10) == 0
        assert proc.stderr.read() == b""
    finally:
        if proc.poll() is None:
            proc.kill()


def test_source_stays_python36_compatible():
    source = COLLECTOR.read_text()
    try:
        ast.parse(source, feature_version=(3, 6))
    except ValueError:  # newer Pythons only parse back to 3.7
        ast.parse(source, feature_version=(3, 7))
    tree = ast.parse(source)
    imports = {alias.name.split(".")[0] for node in ast.walk(tree)
               if isinstance(node, (ast.Import, ast.ImportFrom))
               for alias in getattr(node, "names", [])}
    imports |= {node.module.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module}
    assert imports <= {"json", "os", "pwd", "sys", "time", "argparse"}, imports
    # f-strings with "=" and other 3.8+ features would fail above; also keep 3.6 stdlib only.
    for banned in ("capture_output", "time_ns", "nullcontext", "removeprefix", ":="):
        assert banned not in source


@pytest.mark.skipif(not Path("/proc/stat").exists(), reason="needs Linux /proc")
def test_real_proc_once():
    done = run_collector(str(COLLECTOR), "--once", "--interval", "0.2")
    snap = json.loads(done.stdout)
    assert snap["ncpu"] >= 1 and snap["mem"]["total"] > 0 and snap["procs"]
