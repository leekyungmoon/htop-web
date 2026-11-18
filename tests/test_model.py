"""Host specs, snapshot cleaning, and per-viewer views."""

from __future__ import annotations

import pytest
from conftest import proc_row, raw_snapshot

from htop_web.model import (P_CMD, P_HE, P_HS, P_PID, P_USER, HostSpec, HostState, ViewParams,
                            clean_text, host_view, normalize_snapshot, parse_host,
                            pick_processes, select_hosts)


@pytest.mark.parametrize("text, host, port, user", [
    ("gpu01", "gpu01", None, None),
    ("alice@gpu01", "gpu01", None, "alice"),
    ("alice@gpu01:2222", "gpu01", 2222, "alice"),
    ("192.0.2.10:22", "192.0.2.10", 22, None),
    ("[::1]:2200", "::1", 2200, None),
    ("bob@[fe80::1%eth0]", "fe80::1%eth0", None, "bob"),
    ("::1", "::1", None, None),
    ("MyAlias", "MyAlias", None, None),  # ~/.ssh/config aliases keep their case
])
def test_parse_host(text, host, port, user):
    spec = parse_host(text)
    assert (spec.kind, spec.host, spec.port, spec.username, spec.key) == ("ssh", host, port, user, text)


def test_parse_host_default_port_and_local():
    assert parse_host("gpu01", default_port=2022).port == 2022
    assert parse_host("gpu01:23", default_port=2022).port == 23
    assert parse_host("local") == HostSpec(key="local", kind="local")


@pytest.mark.parametrize("text", ["", "  ", "@gpu01", "gpu01:abc", "gpu01:70000", "gpu01:0",
                                  "a b", "gpu;rm -rf", "x/y", "[::1", "[::1]x", "a$(id)"])
def test_parse_host_rejects(text):
    with pytest.raises(ValueError):
        parse_host(text)


def test_clean_text_removes_controls_and_bidi():
    assert clean_text("a\x1b[31mb\x07c‮d\nz") == "a?[31mb?c?d?z"
    assert clean_text(None) == "" and clean_text(12) == "12"
    assert clean_text("x" * 50, limit=10) == "x" * 10


def test_normalize_snapshot_cleans_and_validates():
    bad = proc_row(66, user="ev\x1bil", cmd="<script>alert(1)</script>\x1b]0;t\x07")
    bad[15], bad[16] = -5, 9999  # highlight range out of bounds
    raw = raw_snapshot(host="h\x1b[2J", procs=[bad, ["not", "enough"], "junk", proc_row(67)])
    snap = normalize_snapshot(raw)
    assert snap["host"] == "h?[2J"
    pids = [p[P_PID] for p in snap["procs"]]
    assert pids == [66, 67]
    row = snap["procs"][pids.index(66)]
    assert row[P_USER] == "ev?il"
    assert row[P_CMD] == "<script>alert(1)</script>?]0;t?"
    assert 0 <= row[P_HS] <= row[P_HE] <= len(row[P_CMD])


@pytest.mark.parametrize("raw", [None, [], {"v": 2}, {"v": 1}, {"v": 1, "proc_fields": ["x"]}])
def test_normalize_snapshot_rejects(raw):
    with pytest.raises(ValueError):
        normalize_snapshot(raw)


def test_normalize_snapshot_tolerates_missing_parts():
    snap = normalize_snapshot({"v": 1, "proc_fields": ["pid", "cmd"], "procs": [[5, "x"]],
                               "load": "bad", "cpu": [float("nan"), 500]})
    assert snap["procs"][0][P_PID] == 5 and snap["cpu"][:2] == [0.0, 100.0]
    assert snap["load"] == [0.0, 0.0, 0.0] and snap["mem"]["total"] == 0


def test_view_params_updates():
    p = ViewParams()
    assert p.updated({"nodes": "a, b,,"}).nodes == ("a", "b")
    assert p.updated({"nodes": ["x"]}).nodes == ("x",)
    assert p.updated({"nodes": ""}).nodes is None
    assert p.updated({"sort": "mem", "order": "asc"}).sort == "mem"
    assert p.updated({"sort": "__class__"}).sort == "cpu"
    assert p.updated({"order": "sideways"}).order == "desc"
    assert p.updated({"rows": "0"}).rows == 1 and p.updated({"rows": 10**9}).rows == 500
    assert p.updated({"rows": "many"}).rows == 15
    assert p.updated({"filter": "  py " + "x" * 500}).filter.startswith("py")
    assert len(p.updated({"filter": "x" * 500}).filter) == 200
    assert p.updated({"kthreads": "yes"}).kthreads and not p.updated({"kthreads": "0"}).kthreads


def test_pick_processes(snapshot):
    procs = snapshot["procs"]
    rows, shown, matched = pick_processes(procs, ViewParams(rows=3))
    assert [r[P_PID] for r in rows] == [109, 108, 107]  # CPU% descending
    assert (shown, matched) == (10, 10)  # the kernel thread is hidden
    rows, shown, _ = pick_processes(procs, ViewParams(rows=50, kthreads=True))
    assert shown == 11 and any(r[P_CMD] == "kthreadd" for r in rows)
    rows, _, matched = pick_processes(procs, ViewParams(filter="JOB3"))
    assert matched == 1 and rows[0][P_PID] == 103
    rows, _, _ = pick_processes(procs, ViewParams(filter="ALICE", rows=50))
    assert len(rows) == 10
    rows, _, _ = pick_processes(procs, ViewParams(sort="mem", order="desc", rows=2))
    assert [r[P_PID] for r in rows] == [100, 101]
    rows, _, _ = pick_processes(procs, ViewParams(sort="command", order="asc", rows=2))
    assert [r[P_CMD] for r in rows] == ["/usr/bin/job0 --n 0", "/usr/bin/job1 --n 1"]


def test_pick_processes_ties_keep_pid_order():
    from htop_web.model import normalize_snapshot as norm
    snap = norm(raw_snapshot(procs=[proc_row(p, cpu=1.0) for p in (30, 10, 20)]))
    for order in ("asc", "desc"):
        rows, _, _ = pick_processes(snap["procs"], ViewParams(order=order))
        assert [r[P_PID] for r in rows] == [10, 20, 30]


def test_host_state_and_views(snapshot):
    seen = []
    state = HostState(HostSpec(key="alice@alpha:22", kind="ssh", host="alpha"), seen.append)
    view = host_view(state, ViewParams())
    assert view["status"] == "loading" and "procs" not in view and view["age"] is None
    state.set_status("connecting", "Connecting ...")
    state.set_status("connecting", "Connecting ...")  # no change, no event
    state.update(snapshot)
    assert len(seen) == 2 and state.status == "ok" and state.message == ""
    view = host_view(state, ViewParams(rows=4), now=state.updated + 3)
    assert view["age"] == 3 and len(view["procs"]) == 4 and view["host"] == "alpha"

    other = HostState(HostSpec(key="beta", kind="ssh", host="beta"))
    hosts = [state, other]
    assert select_hosts(hosts, None) == hosts
    assert select_hosts(hosts, ["beta"]) == [other]
    assert select_hosts(hosts, ["alpha"]) == [state]  # by host name or reported hostname
    assert select_hosts(hosts, ["alice@alpha:22", "nope"]) == [state]
