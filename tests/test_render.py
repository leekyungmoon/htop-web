"""Text and ANSI renderings: htop's layout, units and colors."""

from __future__ import annotations

import re

import pytest
from conftest import proc_row, raw_snapshot

from htop_web.model import HostSpec, HostState, ViewParams, host_view, normalize_snapshot
from htop_web.render import (bar, cpu_columns, human, mem_cell, process_header, process_row,
                             render_text, time_text, to_text, uptime_text)

GIB, MIB, KIB = 2**30, 2**20, 2**10
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.mark.parametrize("nbytes, text", [
    (0, "0K"), (512 * KIB, "512K"), (512 * MIB, "512M"), (int(5.12 * GIB), "5.12G"),
    (int(15.5 * GIB), "15.5G"), (256 * GIB, "256G"), (2 * 1024 * GIB, "2.00T"),
])
def test_human(nbytes, text):
    assert human(nbytes) == text


@pytest.mark.parametrize("cs, text", [
    (0, "0:00.00"), (12345, "2:03.45"), (359999, "59:59.99"), (360000, "1h00:00"),
    (100 * 3600 * 100, "4d04h"), (400 * 86400 * 100, "1y035d"),
])
def test_time_text(cs, text):
    assert time_text(cs) == text


@pytest.mark.parametrize("seconds, text", [
    (59, "00:00:59"), (86400 + 3661, "1 day, 01:01:01"), (3 * 86400, "3 days, 00:00:00"),
    (101 * 86400, "101 days(!), 00:00:00"),
])
def test_uptime_text(seconds, text):
    assert uptime_text(seconds) == text


@pytest.mark.parametrize("nbytes, parts", [
    (999 * KIB, [("  999", None)]),
    (58372 * KIB, [("58", "mega"), ("372", None)]),
    (9548 * KIB, [(" 9", "mega"), ("548", None)]),
    (100352 * KIB, [("98.0M", "mega")]),
    (241 * MIB, [(" 241M", "mega")]),
    (5596 * MIB, [("5", "giga"), ("596M", "mega")]),
    (int(53.35 * GIB), [("53", "giga"), (".3", "mega"), ("G", "giga")]),
    (448 * GIB, [(" 448G", "giga")]),
    (1448 * GIB, [("1", "tera"), ("448G", "giga")]),
])
def test_mem_cell_matches_htop(nbytes, parts):
    assert mem_cell(nbytes) == parts
    assert sum(len(t) for t, _ in parts) == 5


def test_bar_fills_with_ceil_and_overlays_text():
    assert bar(10, [(50, "user")], 100, "50%") == [("|||||", "user"), ("  50%", "shadow")]
    assert bar(5, [(100, "user")], 100, "100%") == [("|100%", "user")]
    segs = bar(10, [(1, "nice"), (1, "user"), (0, "system")], 100, "")
    assert segs == [("|", "nice"), ("|", "user"), ("        ", "shadow")]
    assert bar(0, [(1, "x")], 1, "t") == []
    assert bar(4, [(5, "user")], 0, "abcdef") == [("cdef", "shadow")]


def test_cpu_columns():
    assert [cpu_columns(n) for n in (1, 8, 16, 17, 32, 33, 64, 128)] == [1, 1, 1, 2, 2, 4, 4, 4]


def test_header_matches_htop_layout():
    header = to_text(process_header(ViewParams(), 75), color=False).rstrip()
    assert header == "    PID USER       PRI  NI  VIRT   RES   SHR S  CPU%▽MEM%   TIME+  Command"
    asc = to_text(process_header(ViewParams(sort="pid", order="asc"), 75), color=False)
    assert asc.startswith("    PID△USER")


def test_row_lines_up_with_the_header(snapshot):
    row = list(normalize_snapshot(raw_snapshot(procs=[proc_row(
        1132531, "alice", 17.0, 0.0, "python3", state="R", res=9548 * KIB, time=26)]))["procs"][0])
    text = to_text(process_row(row, 200), color=False)
    header = to_text(process_header(ViewParams(), 200), color=False)
    assert text.startswith("1132531 alice       20   0 38192  9548  2387 R  17.0  0.0  0:00.26 python3")
    assert text.index("python3") == header.index("Command") == 67
    assert text[45] == "R" and header[45] == "S"


def test_row_colors():
    row = list(normalize_snapshot(raw_snapshot(procs=[proc_row(5, nice=-5, state="D")]))["procs"][0])
    segs = process_row(row, 200)
    styles = {text.strip(): style for text, style in segs if text.strip()}
    assert styles["-5"] == "prio_high" and styles["D"] == "state_d"


def view_for(snapshot, key="alpha", status="ok"):
    state = HostState(HostSpec(key=key, kind="ssh", host=key))
    state.update(snapshot)
    if status != "ok":
        state.set_status(status, "Disconnected: gone")
    return host_view(state, ViewParams(rows=5), now=state.updated + 1)


def test_render_text_plain_and_ansi(snapshot):
    views = [view_for(snapshot)]
    plain = render_text(views, ViewParams(rows=5), width=100)
    lines = plain.splitlines()
    assert lines[0].startswith("alpha  live  updated 1.0s ago  Linux 6.1.0-test")
    assert any(line.startswith("  0[") for line in lines)
    assert any(line.startswith("Mem[") and "5.25G/16.0G]" in line for line in lines)
    assert any("Tasks: 10, 30 thr, 1 kthr; 1 running" in line for line in lines)
    assert any("Load average: 0.50 0.40 0.30" in line for line in lines)
    assert any("Uptime: 1 day, 01:01:01" in line for line in lines)
    assert "5 of 10 processes; kernel threads hidden" in plain
    assert "\x1b" not in plain and all(len(line) <= 100 for line in lines)

    ansi = render_text(views, ViewParams(rows=5), width=100, color=True)
    assert "\x1b[30;42m" in ansi and "\x1b[30;46m" in ansi  # header and sorted column
    assert [line.rstrip() for line in ANSI.sub("", ansi).splitlines()] == lines


def test_render_text_shows_errors_and_empty(snapshot):
    text = render_text([view_for(snapshot, status="error")], ViewParams(), width=80)
    assert "error" in text.splitlines()[0] and "Disconnected: gone" in text
    loading = host_view(HostState(HostSpec(key="beta", kind="ssh", host="beta")), ViewParams())
    assert render_text([loading], ViewParams()).splitlines() == ["beta  loading", "Loading ..."]
    assert render_text([], ViewParams()) == "(no hosts)\n"


def test_render_text_never_passes_terminal_escapes():
    evil = "<b>x</b> \x1b]0;owned\x07 \x1b[2J \x9b31m"
    snap = normalize_snapshot(raw_snapshot(host="ho\x1bst", procs=[proc_row(9, user="u\x1b", cmd=evil)]))
    view = view_for(snap)
    text = render_text([view], ViewParams(), width=200, color=True)
    assert "\x07" not in text and "\x9b" not in text and "\x1b]" not in text
    assert "\x1b[2J" not in text and "<b>x</b> ?]0;owned? ?[2J ?31m" in text
    assert ANSI.sub("", text).count("\x1b") == 0


def test_many_cpus_layout():
    snap = normalize_snapshot(raw_snapshot(ncpu=64))
    text = render_text([view_for(snap)], ViewParams(), width=160)
    lines = text.splitlines()
    assert sum(line.count("[") for line in lines[1:9]) == 64
    assert all(len(line) <= 160 for line in lines)
