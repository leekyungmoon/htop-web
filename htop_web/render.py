"""Plain-text and ANSI renderings of host views, for /htop.txt and /htop.ansi.

The layout and colors follow htop 3's defaults. The page (static/app.js) draws
the same things with the same rules.
"""

from __future__ import annotations

import math
from itertools import zip_longest

from .model import (P_CMD, P_CPU, P_MEM, P_NI, P_PID, P_PRI, P_RES, P_SHR, P_STATE, P_TIME,
                    P_USER, P_VIRT, ViewParams, clean_text)

# SGR codes per style, after htop's default color scheme.
ANSI = {
    "label": "36", "border": "1", "shadow": "1;90", "value": "1;36",
    "nice": "34", "user": "32", "system": "31", "virt": "36",
    "mem_used": "32", "mem_shared": "35", "mem_buffers": "1;34", "mem_cache": "33",
    "swap_used": "31", "swap_cache": "33",
    "threads": "1;32", "running": "1;32",
    "load1": "1", "load5": "1;36", "load15": "36", "uptime": "1;36",
    "header": "30;42", "header_sort": "30;46",
    "title": "1", "dim": "1;90", "ok": "32", "warn": "33", "error": "31",
    "mega": "36", "giga": "32", "tera": "1;31",
    "state_r": "32", "state_d": "1;31", "prio_high": "31", "prio_low": "32",
}

Segments = list  # of (text, style or None)


# ----------------------------------------------------------------- formatting

def human(nbytes: float) -> str:
    """A byte count the way htop's meters print it: 0K, 512M, 5.12G, 15.5G."""
    value = max(float(nbytes), 0.0) / 1024.0
    unit = "K"
    for next_unit in "MGTPE":
        if value < 1024:
            break
        value /= 1024.0
        unit = next_unit
    if value == 0:
        return "0" + unit
    if value < 9.995:
        return f"{value:.2f}{unit}"
    if value < 99.95:
        return f"{value:.1f}{unit}"
    return f"{value:.0f}{unit}"


def uptime_text(seconds: float) -> str:
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, s = divmod(s, 60)
    clock = f"{hours:02d}:{minutes:02d}:{s:02d}"
    if days > 100:
        return f"{days} days(!), {clock}"
    if days > 1:
        return f"{days} days, {clock}"
    if days == 1:
        return f"1 day, {clock}"
    return clock


def time_text(centiseconds: int) -> str:
    """CPU time like htop's TIME+ column: 1:23.45, 12h34:56, 3d04h."""
    total = int(centiseconds) // 100
    if total < 3600:
        minutes, seconds = divmod(total, 60)
        return f"{minutes}:{seconds:02d}.{int(centiseconds) % 100:02d}"
    hours, rest = divmod(total, 3600)
    if hours < 100:
        minutes, seconds = divmod(rest, 60)
        return f"{hours}h{minutes:02d}:{seconds:02d}"
    days, hours = divmod(hours, 24)
    if days < 365:
        return f"{days}d{hours:02d}h"
    years, days = divmod(days, 365)
    return f"{years}y{days:03d}d"


def mem_cell(nbytes: int) -> Segments:
    """A 5-wide memory column: 58372 (KiB) 241M 5596M 53.3G 1448G, colored by magnitude."""
    kib = int(nbytes) // 1024
    if kib < 1000:
        return [(f"{kib:5d}", None)]
    if kib < 100000:
        return [(f"{kib // 1000:2d}", "mega"), (f"{kib % 1000:03d}", None)]
    mib = kib / 1024
    if mib < 1000:
        return [(f"{mib:4.0f}M" if mib >= 99.95 else f"{mib:4.1f}M", "mega")]
    if mib < 10000:
        whole = int(mib)
        return [(str(whole // 1000), "giga"), (f"{whole % 1000:03d}M", "mega")]
    gib = mib / 1024
    if gib < 100:
        tenths = int(gib * 10)
        return [(f"{tenths // 10:2d}", "giga"), (f".{tenths % 10}", "mega"), ("G", "giga")]
    if gib < 1000:
        return [(f"{int(gib):4d}G", "giga")]
    if gib < 10000:
        whole = int(gib)
        return [(str(whole // 1000), "tera"), (f"{whole % 1000:03d}G", "giga")]
    return [(f"{gib / 1024:4.1f}T", "tera")]


def cpu_parts(values: list[float]) -> list[tuple[float, str]]:
    """htop's default CPU bar: low-priority, normal, kernel, virtualization."""
    user, nice, system, irq, softirq, _iowait, steal, guest = values[:8]
    return [(nice, "nice"), (user, "user"), (system + irq + softirq, "system"),
            (steal + guest, "virt")]


def cpu_total(values: list[float]) -> float:
    return min(sum(v for v, _ in cpu_parts(values)), 100.0)


def mem_parts(mem: dict) -> list[tuple[float, str]]:
    return [(mem["used"], "mem_used"), (mem["shared"], "mem_shared"),
            (mem["buffers"], "mem_buffers"), (mem["cache"], "mem_cache")]


def mem_text(mem: dict) -> str:
    return f"{human(mem['used'] + mem['shared'])}/{human(mem['total'])}"


def cpu_columns(ncpu: int) -> int:
    """How many CPU meters share a row in each half (the page uses the same rule)."""
    half = math.ceil(ncpu / 2)
    return 1 if half <= 8 else 2 if half <= 16 else 4


# ---------------------------------------------------------------------- meters

def bar(width: int, parts, total: float, text: str) -> Segments:
    """The inside of a bar meter.

    Each part fills ceil(value / total * width) cells with '|' in its color. The
    value sits at the right end; under a filled part it takes that part's color.
    """
    if width <= 0:
        return []
    text = text[-width:]
    chars = [" "] * (width - len(text)) + list(text)
    styles = ["shadow"] * width
    offset = 0
    for value, style in parts:
        if total <= 0 or value <= 0:
            continue
        cells = math.ceil(min(value, total) / total * width - 1e-9)
        end = min(offset + cells, width)
        for i in range(offset, end):
            if chars[i] == " ":
                chars[i] = "|"
            styles[i] = style
        offset = end
    segs: Segments = []
    for ch, style in zip(chars, styles):
        if segs and segs[-1][1] == style:
            segs[-1] = (segs[-1][0] + ch, style)
        else:
            segs.append((ch, style))
    return segs


def meter(label: str, width: int, parts, total: float, text: str) -> Segments:
    inner = width - len(label) - 2
    return [(label, "label"), ("[", "border")] + bar(inner, parts, total, text) + [("]", "border")]


def seg_width(segs: Segments) -> int:
    return sum(len(t) for t, _ in segs)


def fit(segs: Segments, width: int, fill: bool = True) -> Segments:
    """Cut segments to width; with fill, pad short ones with spaces."""
    out, left = [], width
    for text, style in segs:
        if left <= 0:
            break
        out.append((text[:left], style))
        left -= len(text[:left])
    if fill and left > 0:
        out.append((" " * left, None))
    return out


def cpu_meters(cpus: list[list], width: int, per_row: int, label_width: int) -> list[Segments]:
    cell = (width - (per_row - 1)) // per_row
    lines = []
    for i in range(0, len(cpus), per_row):
        line: Segments = []
        for j, entry in enumerate(cpus[i:i + per_row]):
            if j:
                line.append((" ", None))
            values = entry[1:]
            line += meter(str(entry[0]).rjust(label_width), cell, cpu_parts(values), 100.0,
                          f"{cpu_total(values):.1f}%")
        lines.append(line)
    return lines


def tasks_line(tasks: dict) -> Segments:
    return [("Tasks: ", "label"), (str(tasks["procs"]), "value"), (", ", "label"),
            (str(tasks["threads"]), "threads"), (" thr", "label"),
            (f", {tasks['kthreads']} kthr", "shadow"), ("; ", "label"),
            (str(tasks["running"]), "running"), (" running", "label")]


def load_line(load: list[float]) -> Segments:
    return [("Load average: ", "label"), (f"{load[0]:.2f} ", "load1"),
            (f"{load[1]:.2f} ", "load5"), (f"{load[2]:.2f}", "load15")]


def header_lines(view: dict, width: int) -> list[Segments]:
    """CPUs, Mem and Swp on the left; more CPUs, Tasks, Load and Uptime on the right."""
    gap = 2
    col = max((width - gap) // 2, 24)
    cpus = view["cpus"]
    half = math.ceil(len(cpus) / 2)
    per_row = cpu_columns(len(cpus))
    digits = len(str(max([int(c[0]) for c in cpus] or [0])))
    label_width = max(3 if per_row == 1 else 2, digits)
    left = cpu_meters(cpus[:half], col, per_row, label_width)
    right = cpu_meters(cpus[half:], col, per_row, label_width)
    mem, swap = view["mem"], view["swap"]
    left.append(meter("Mem", col, mem_parts(mem), mem["total"], mem_text(mem)))
    left.append(meter("Swp", col, [(swap["used"], "swap_used"), (swap["cache"], "swap_cache")],
                      swap["total"], f"{human(swap['used'])}/{human(swap['total'])}"))
    right.append(tasks_line(view["tasks"]))
    right.append(load_line(view["load"]))
    right.append([("Uptime: ", "label"), (uptime_text(view["uptime"]), "uptime")])
    return [fit(lft or [], col) + [(" " * gap, None)] + fit(rgt or [], col, fill=False)
            for lft, rgt in zip_longest(left, right)]


# ----------------------------------------------------------------- process table

# sort key, header text (already padded to the column width), right-aligned
COLUMNS = [
    ("pid", "    PID", 7), ("user", "USER      ", 10), ("pri", "PRI", 3), ("ni", " NI", 3),
    ("virt", " VIRT", 5), ("res", "  RES", 5), ("shr", "  SHR", 5), ("state", "S", 1),
    ("cpu", " CPU%", 5), ("mem", "MEM%", 4), ("time", "  TIME+ ", 8), ("command", "Command", 0),
]


def process_header(params: ViewParams, width: int) -> Segments:
    arrow = "▽" if params.order == "desc" else "△"  # htop's sort marker
    segs: Segments = []
    for i, (key, title, _size) in enumerate(COLUMNS):
        sorted_here = key == params.sort
        sep = arrow if sorted_here else " "
        style = "header_sort" if sorted_here else "header"
        last = i == len(COLUMNS) - 1
        segs.append((title, style))
        if not last or sorted_here:
            segs.append((sep, style))
    used = seg_width(segs)
    if used < width:
        segs.append((" " * (width - used), "header"))
    return fit(segs, width)


def process_row(p: list, width: int) -> Segments:
    pri = "RT" if p[P_PRI] <= -100 else str(p[P_PRI])
    ni, state, cpu = p[P_NI], p[P_STATE], p[P_CPU]
    segs: Segments = [
        (f"{p[P_PID]:7d} ", None), (p[P_USER][:10].ljust(10) + " ", None),
        (f"{pri:>3} ", None),
        (f"{ni:3d}", "prio_high" if ni < 0 else "prio_low" if ni > 0 else "shadow"), (" ", None),
    ]
    for col in (P_VIRT, P_RES, P_SHR):
        segs += mem_cell(p[col]) + [(" ", None)]
    segs += [
        (state, "state_r" if state == "R" else "state_d" if state == "D"
         else "shadow" if state in "SI" else None), (" ", None),
        (f"{cpu:5.1f} " if cpu < 999.95 else f"{cpu:5.0f} ", None),
        (f"{p[P_MEM]:4.1f} ", None),
        (f"{time_text(p[P_TIME]):>8} ", None),
        (p[P_CMD], None),
    ]
    return fit(segs, width, fill=False)


STATUS_STYLE = {"ok": "ok", "stale": "warn", "connecting": "warn", "loading": "dim",
                "error": "error"}


def render_host(view: dict, params: ViewParams, width: int) -> list[Segments]:
    name = view.get("host") or view["key"]
    title: Segments = [(name, "title")]
    if view["key"] not in (name, "local"):
        title.append((f" ({view['key']})", "dim"))
    if view["kind"] in ("local", "demo"):
        title.append((f" [{view['kind']}]", "dim"))
    status = view["status"]
    title.append(("  " + ("live" if status == "ok" else status), STATUS_STYLE.get(status)))
    if view.get("age") is not None:
        title.append((f"  updated {view['age']:.1f}s ago", "dim"))
    if view.get("kernel"):
        title.append((f"  Linux {view['kernel']}", "dim"))
    lines = [fit(title, width, fill=False)]
    if view.get("message"):
        lines.append([(clean_text(view["message"], width), STATUS_STYLE.get(status))])
    if "cpus" not in view:
        return lines
    lines += header_lines(view, width)
    lines.append([])
    lines.append(process_header(params, width))
    lines += [process_row(p, width) for p in view["procs"]]
    note = f"{len(view['procs'])} of {view['matched']} processes"
    if params.filter:
        note += f" matching {params.filter!r} ({view['shown']} in all)"
    if not params.kthreads:
        note += "; kernel threads hidden"
    lines.append([(note, "dim")])
    return lines


def to_text(segs: Segments, color: bool) -> str:
    if not color:
        return "".join(t for t, _ in segs)
    out = []
    for text, style in segs:
        code = ANSI.get(style) if style else None
        # Spaces need color only where it is a background (the table header).
        if code and (text.strip() or style.startswith("header")):
            out.append(f"\x1b[{code}m{text}\x1b[0m")
        else:
            out.append(text)
    return "".join(out)


def render_text(views: list[dict], params: ViewParams, width: int = 120,
                color: bool = False) -> str:
    """Render hosts for a terminal. Strings were cleaned by normalize_snapshot."""
    width = min(max(int(width), 60), 400)
    out: list[str] = []
    for i, view in enumerate(views):
        if i:
            out.append("")
        out += [to_text(line, color).rstrip() for line in render_host(view, params, width)]
    if not views:
        out.append("(no hosts)")
    return "\n".join(out) + "\n"
