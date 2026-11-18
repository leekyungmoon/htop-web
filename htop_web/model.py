"""Host specs, snapshot cleaning, and the per-viewer process view."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping

# Columns of a process row, as stored on the server and sent to browsers.
PROC_FIELDS = ("pid", "ppid", "user", "pri", "ni", "virt", "res", "shr", "state",
               "cpu", "mem", "time", "nlwp", "kthread", "cmd", "hs", "he")
(P_PID, P_PPID, P_USER, P_PRI, P_NI, P_VIRT, P_RES, P_SHR, P_STATE,
 P_CPU, P_MEM, P_TIME, P_NLWP, P_KTHREAD, P_CMD, P_HS, P_HE) = range(len(PROC_FIELDS))

CPU_FIELDS = ("user", "nice", "system", "irq", "softirq", "iowait", "steal", "guest")

# Sort keys a viewer may ask for, and the row column each one sorts by.
SORT_COLUMNS = {
    "pid": P_PID, "user": P_USER, "pri": P_PRI, "ni": P_NI, "virt": P_VIRT,
    "res": P_RES, "shr": P_SHR, "state": P_STATE, "cpu": P_CPU, "mem": P_MEM,
    "time": P_TIME, "command": P_CMD,
}
TEXT_COLUMNS = {P_USER, P_STATE, P_CMD}
MAX_ROWS = 500
MAX_FILTER = 200

# Control characters, and the invisible marks that can reorder text on screen.
_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f؜‎‏‪-‮⁦-⁩]")


def clean_text(value: Any, limit: int = 4096) -> str:
    """Make a string from a host safe to print: no control or bidi characters."""
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    return _UNSAFE.sub("?", value[:limit])


# --------------------------------------------------------------------------- hosts

@dataclass(frozen=True)
class HostSpec:
    """One host to watch, as given on the command line."""

    key: str  # unique; what ?nodes= matches and what the page shows
    kind: str  # "local", "ssh" or "demo"
    host: str | None = None
    port: int | None = None
    username: str | None = None


def parse_host(text: str, default_port: int | None = None) -> HostSpec:
    """Parse ``[USER@]HOST[:PORT]``, ``[USER@][IPV6]:PORT`` or ``local``."""
    spec = text.strip()
    if not spec:
        raise ValueError("empty host")
    if spec == "local":
        return HostSpec(key="local", kind="local")
    if any(c.isspace() or c in "/\\'\"`$;|&<>" for c in spec):
        raise ValueError(f"invalid host {text!r}")
    username, sep, rest = spec.rpartition("@")
    if not sep:
        username, rest = "", spec
    elif not username:
        raise ValueError(f"invalid user in {text!r}")
    port_text = ""
    if rest.startswith("["):  # [IPv6] or [IPv6]:port
        end = rest.find("]")
        if end < 0:
            raise ValueError(f"invalid host {text!r}")
        host, after = rest[1:end], rest[end + 1:]
        if after:
            if not after.startswith(":"):
                raise ValueError(f"invalid host {text!r}")
            port_text = after[1:]
    elif rest.count(":") == 1:
        host, port_text = rest.split(":")
    else:
        host = rest  # a name, IPv4, or a bare IPv6 address without a port
    if not host:
        raise ValueError(f"invalid host {text!r}")
    port = None
    if port_text:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise ValueError(f"invalid port in {text!r}")
        port = int(port_text)
    return HostSpec(key=spec, kind="ssh", host=host, port=port or default_port,
                    username=username or None)


# ------------------------------------------------------------------- snapshots

def _num(value: Any, kind: type = float, default: Any = 0) -> Any:
    try:
        number = kind(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if isinstance(number, float) and not math.isfinite(number):
        return default
    return number


def _percents(values: Any) -> list[float]:
    values = values if isinstance(values, list) else []
    out = [min(max(_num(v), 0.0), 100.0) for v in values[:len(CPU_FIELDS)]]
    return out + [0.0] * (len(CPU_FIELDS) - len(out))


def normalize_snapshot(raw: Any) -> dict:
    """Validate a collector snapshot and clean every string in it.

    Hosts are not fully trusted: a broken or hostile collector must not be able
    to crash the server or inject markup or terminal escapes into what viewers see.
    """
    if not isinstance(raw, dict) or raw.get("v") != 1:
        raise ValueError("not an htop-web snapshot")
    fields = raw.get("proc_fields")
    if not isinstance(fields, list):
        raise ValueError("snapshot has no proc_fields")
    index = {name: i for i, name in enumerate(fields) if isinstance(name, str)}
    missing = [name for name in ("pid", "cmd") if name not in index]
    if missing:
        raise ValueError(f"snapshot lacks {missing}")

    def column(row: list, name: str, kind: type, default: Any) -> Any:
        i = index.get(name)
        return default if i is None or i >= len(row) else _num(row[i], kind, default)

    def text(row: list, name: str, limit: int) -> str:
        i = index.get(name)
        return "" if i is None or i >= len(row) else clean_text(row[i], limit)

    procs = []
    for row in raw.get("procs") or []:
        if not isinstance(row, list) or column(row, "pid", int, 0) <= 0:
            continue
        cmd = text(row, "cmd", 4096)
        hs = min(max(column(row, "hs", int, 0), 0), len(cmd))
        he = min(max(column(row, "he", int, 0), hs), len(cmd))
        procs.append((
            column(row, "pid", int, 0), column(row, "ppid", int, 0),
            text(row, "user", 64), column(row, "pri", int, 0), column(row, "ni", int, 0),
            max(column(row, "virt", int, 0), 0), max(column(row, "res", int, 0), 0),
            max(column(row, "shr", int, 0), 0), text(row, "state", 1) or "?",
            max(column(row, "cpu", float, 0.0), 0.0), max(column(row, "mem", float, 0.0), 0.0),
            max(column(row, "time", int, 0), 0), max(column(row, "nlwp", int, 1), 0),
            1 if column(row, "kthread", int, 0) else 0, cmd, hs, he,
        ))

    def memory(name: str, keys: Iterable[str]) -> dict:
        source = raw.get(name) if isinstance(raw.get(name), dict) else {}
        return {k: max(_num(source.get(k), int, 0), 0) for k in keys}

    cpus = []
    for entry in raw.get("cpus") or []:
        if isinstance(entry, list) and entry:
            cpus.append([_num(entry[0], int, len(cpus))] + _percents(entry[1:]))
    load = raw.get("load") if isinstance(raw.get("load"), list) else []
    tasks = raw.get("tasks") if isinstance(raw.get("tasks"), dict) else {}
    return {
        "host": clean_text(raw.get("host"), 128) or "?",
        "kernel": clean_text(raw.get("kernel"), 128),
        "time": _num(raw.get("time")),
        "ncpu": len(cpus),
        "cpu": _percents(raw.get("cpu")),
        "cpus": cpus,
        "mem": memory("mem", ("total", "used", "buffers", "shared", "cache", "available")),
        "swap": memory("swap", ("total", "used", "cache")),
        "load": [max(_num(v), 0.0) for v in (load + [0, 0, 0])[:3]],
        "uptime": max(_num(raw.get("uptime")), 0.0),
        "tasks": {k: max(_num(tasks.get(k), int, 0), 0)
                  for k in ("procs", "threads", "kthreads", "running")},
        "procs": procs,
    }


class HostState:
    """What we currently know about one host."""

    def __init__(self, spec: HostSpec, on_change: Callable[[HostState], None] | None = None):
        self.spec = spec
        self.status = "loading"  # loading | connecting | ok | stale | error
        self.message = "Loading ..."
        self.snapshot: dict | None = None
        self.updated: float | None = None  # time.time() of the last snapshot
        self.seq = 0
        self._on_change = on_change

    def _changed(self) -> None:
        self.seq += 1
        if self._on_change is not None:
            self._on_change(self)

    def set_status(self, status: str, message: str = "") -> None:
        if (status, message) != (self.status, self.message):
            self.status, self.message = status, message
            self._changed()

    def update(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        self.updated = time.time()
        self.status, self.message = "ok", ""
        self._changed()


# ----------------------------------------------------------------------- views

def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


@dataclass(frozen=True)
class ViewParams:
    """How one viewer wants to see the hosts."""

    nodes: tuple[str, ...] | None = None
    sort: str = "cpu"
    order: str = "desc"
    filter: str = ""
    rows: int = 15
    kthreads: bool = False

    def updated(self, data: Mapping[str, Any]) -> ViewParams:
        """Return a copy changed by a query string or a websocket message."""
        changes: dict[str, Any] = {}
        if "nodes" in data:
            nodes = data["nodes"]
            if isinstance(nodes, str):
                nodes = [n for n in nodes.split(",")]
            if isinstance(nodes, list):
                names = tuple(n.strip() for n in nodes if isinstance(n, str) and n.strip())
                changes["nodes"] = names or None
            elif nodes is None:
                changes["nodes"] = None
        sort = data.get("sort")
        if isinstance(sort, str) and sort in SORT_COLUMNS:
            changes["sort"] = sort
        order = data.get("order")
        if order in ("asc", "desc"):
            changes["order"] = order
        if isinstance(data.get("filter"), str):
            changes["filter"] = data["filter"].strip()[:MAX_FILTER]
        if "rows" in data:
            rows = _num(data["rows"], int, self.rows)
            changes["rows"] = min(max(rows, 1), MAX_ROWS)
        if "kthreads" in data:
            changes["kthreads"] = _flag(data["kthreads"])
        return replace(self, **changes)

    def as_dict(self) -> dict:
        return {"nodes": list(self.nodes) if self.nodes else None, "sort": self.sort,
                "order": self.order, "filter": self.filter, "rows": self.rows,
                "kthreads": self.kthreads}


def select_hosts(hosts: Iterable[HostState], nodes: Iterable[str] | None) -> list[HostState]:
    """Keep the hosts named in nodes (by spec, host name, or reported hostname)."""
    hosts = list(hosts)
    if not nodes:
        return hosts
    wanted = set(nodes)
    picked = []
    for state in hosts:
        names = {state.spec.key, state.spec.host}
        if state.snapshot:
            names.add(state.snapshot["host"])
        if names & wanted:
            picked.append(state)
    return picked


def pick_processes(procs: list[tuple], params: ViewParams) -> tuple[list[tuple], int, int]:
    """Filter and sort a process list. Returns (top rows, shown total, matched)."""
    if not params.kthreads:
        procs = [p for p in procs if not p[P_KTHREAD]]
    shown = len(procs)
    if params.filter:
        needle = params.filter.lower()
        procs = [p for p in procs if needle in p[P_CMD].lower() or needle in p[P_USER].lower()]
    col = SORT_COLUMNS[params.sort]
    descending = params.order == "desc"
    if col in TEXT_COLUMNS:
        def key(p: tuple) -> tuple:
            return (p[col].lower(), -p[P_PID] if descending else p[P_PID])
    else:
        def key(p: tuple) -> tuple:
            return (p[col], -p[P_PID] if descending else p[P_PID])
    # Ties keep ascending PIDs either way.
    ordered = sorted(procs, key=key, reverse=descending)
    return ordered[:params.rows], shown, len(procs)


def host_view(state: HostState, params: ViewParams, now: float | None = None) -> dict:
    """Everything a viewer needs to draw one host."""
    now = time.time() if now is None else now
    spec = state.spec
    view: dict[str, Any] = {
        "key": spec.key, "kind": spec.kind, "status": state.status,
        "message": state.message, "seq": state.seq,
        "age": None if state.updated is None else round(max(now - state.updated, 0.0), 2),
    }
    snap = state.snapshot
    if snap is not None:
        rows, shown, matched = pick_processes(snap["procs"], params)
        view.update({
            "host": snap["host"], "kernel": snap["kernel"], "ncpu": snap["ncpu"],
            "cpu": snap["cpu"], "cpus": snap["cpus"], "mem": snap["mem"],
            "swap": snap["swap"], "load": snap["load"], "uptime": snap["uptime"],
            "tasks": snap["tasks"], "procs": [list(p) for p in rows],
            "shown": shown, "matched": matched,
        })
    return view

