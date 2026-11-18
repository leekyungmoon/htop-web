#!/usr/bin/env python3
"""htop-web collector: sample Linux /proc and print one JSON snapshot per interval.

htop-web sends this file to every host over SSH and runs it with
``python3 -u - [options]``, so nothing has to be installed on the hosts.
Keep it self-contained: standard library only, Python 3.6+ syntax.

Try it by hand:

    python3 collector.py --once
"""

import json
import os
import pwd
import sys
import time

SCHEMA = 1
PF_KTHREAD = 0x00200000  # task flag set on kernel threads
MAX_CMD = 4096  # longer command lines are cut

# Percent of CPU time per category, in this order.
CPU_FIELDS = ["user", "nice", "system", "irq", "softirq", "iowait", "steal", "guest"]

# One row per process, in this order. "hs"/"he" mark the program basename in "cmd".
PROC_FIELDS = ["pid", "ppid", "user", "pri", "ni", "virt", "res", "shr", "state",
               "cpu", "mem", "time", "nlwp", "kthread", "cmd", "hs", "he"]


def _sysconf(name, default):
    try:
        value = os.sysconf(name)
    except (ValueError, OSError, AttributeError):
        return default
    return value if value > 0 else default


def cpu_percentages(before, after):
    """Split the time between two /proc/stat "cpu" lines into CPU_FIELDS (percent)."""
    if before is None or after is None:
        return [0.0] * len(CPU_FIELDS)
    d = [max(b - a, 0) for a, b in zip(before, after)]
    user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice = d
    # The kernel also counts guest time in user and nice.
    user = max(user - guest, 0)
    nice = max(nice - guest_nice, 0)
    total = user + nice + system + idle + iowait + irq + softirq + steal + guest + guest_nice
    if total <= 0:
        return [0.0] * len(CPU_FIELDS)
    scale = 100.0 / total
    return [round(v * scale, 2)
            for v in (user, nice, system, irq, softirq, iowait, steal, guest + guest_nice)]


def basename_span(cmd, argv0):
    """Return (start, end) of the program name inside cmd, like htop highlights it."""
    token = argv0
    space = token.find(" ")
    if space >= 0:  # a rewritten title such as "sshd: alice@pts/0"
        token = token[:space]
    if token.endswith(":"):
        token = token[:-1]
    start = token.rfind("/") + 1
    end = min(len(token), len(cmd))
    if end <= start:
        return 0, 0
    return start, end


class Sampler(object):
    """Read raw counters from a /proc tree and turn two samples into a snapshot."""

    def __init__(self, proc="/proc"):
        self.proc = proc
        self.clk_tck = _sysconf("SC_CLK_TCK", 100)
        self.page_size = _sysconf("SC_PAGE_SIZE", 4096)
        self._users = {}
        self._cmds = {}

    def _read(self, *parts):
        with open(os.path.join(self.proc, *parts), "rb") as f:
            return f.read()

    def _text(self, *parts):
        return self._read(*parts).decode("utf-8", "replace")

    def user_name(self, uid):
        name = self._users.get(uid)
        if name is None:
            try:
                name = pwd.getpwuid(uid).pw_name
            except (KeyError, OverflowError):
                name = str(uid)
            self._users[uid] = name
        return name

    def sample(self):
        """Read the counters that need two samples (CPU times)."""
        now = time.monotonic()
        total = None
        cpus = {}
        running = 0
        for line in self._text("stat").splitlines():
            if line.startswith("cpu"):
                parts = line.split()
                values = [int(v) for v in parts[1:11]]
                values += [0] * (10 - len(values))
                if parts[0] == "cpu":
                    total = values
                else:
                    cpus[int(parts[0][3:])] = values
            elif line.startswith("procs_running"):
                running = int(line.split()[1])

        procs = {}
        cmds = {}
        for name in os.listdir(self.proc):
            if name.isdigit():
                info = self._read_process(name, cmds)
                if info is not None:
                    procs[(info[0], info[1])] = info
        self._cmds = cmds
        return {"t": now, "wall": time.time(), "cpu": total, "cpus": cpus,
                "running": running, "procs": procs}

    def _read_process(self, name, cmds):
        try:
            uid = os.stat(os.path.join(self.proc, name)).st_uid
            stat = self._read(name, "stat")
            statm = self._read(name, "statm").split()
        except (OSError, IOError):
            return None  # the process exited, or we may not read it
        try:
            lp = stat.index(b"(")
            rp = stat.rindex(b")")
            pid = int(stat[:lp])
            comm = stat[lp + 1:rp].decode("utf-8", "replace")
            f = stat[rp + 2:].split()
            state = f[0].decode("ascii", "replace")
            ppid = int(f[1])
            flags = int(f[6])
            ticks = int(f[11]) + int(f[12])  # utime + stime
            priority = int(f[15])
            nice = int(f[16])
            nlwp = int(f[17])
            start = int(f[19])
            size, resident, shared = int(statm[0]), int(statm[1]), int(statm[2])
        except (ValueError, IndexError):
            return None
        kthread = bool(flags & PF_KTHREAD) or pid == 2 or ppid == 2
        key = (pid, start)
        cmd = self._cmds.get(key)
        if cmd is None:
            cmd = self._command(name, comm, kthread)
        cmds[key] = cmd
        return (pid, start, ppid, uid, state, priority, nice, nlwp, ticks,
                size, resident, shared, kthread, cmd)

    def _command(self, name, comm, kthread):
        args = []
        if not kthread:
            try:
                args = self._read(name, "cmdline").split(b"\0")
            except (OSError, IOError):
                args = []
            while args and not args[-1]:
                args.pop()
        if not args:  # kernel threads and zombies have no command line
            return comm, 0, len(comm)
        argv0 = args[0].decode("utf-8", "replace")
        cmd = " ".join(a.decode("utf-8", "replace") for a in args)[:MAX_CMD]
        hs, he = basename_span(cmd, argv0)
        return cmd, hs, he

    def _memory(self):
        info = {}
        for line in self._text("meminfo").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(":"):
                try:
                    info[parts[0][:-1]] = int(parts[1]) * 1024
                except ValueError:
                    pass
        total = info.get("MemTotal", 0)
        free = info.get("MemFree", 0)
        buffers = info.get("Buffers", 0)
        cached = info.get("Cached", 0)  # includes Shmem
        reclaimable = info.get("SReclaimable", 0)
        shared = info.get("Shmem", 0)
        available = info.get("MemAvailable", free + buffers + cached)
        used = total - free - buffers - cached - reclaimable
        if used < 0:
            used = total - free
        mem = {"total": total, "used": used, "buffers": buffers, "shared": shared,
               "cache": max(cached + reclaimable - shared, 0),
               "available": min(available, total), "free": free}
        swap_total = info.get("SwapTotal", 0)
        swap_cached = info.get("SwapCached", 0)
        swap_used = max(swap_total - info.get("SwapFree", 0) - swap_cached, 0)
        swap = {"total": swap_total, "used": swap_used, "cache": swap_cached}
        return mem, swap

    def _host_text(self, name, default):
        try:
            return self._text("sys", "kernel", name).strip() or default
        except (OSError, IOError):
            return default

    def snapshot(self, prev, cur):
        """Build the JSON-ready snapshot from two samples."""
        dt = max(cur["t"] - prev["t"], 1e-6)
        mem, swap = self._memory()
        load = self._text("loadavg").split()
        uptime = float(self._text("uptime").split()[0])
        cpus = [[i] + cpu_percentages(prev["cpus"].get(i), values)
                for i, values in sorted(cur["cpus"].items())]
        ncpu = max(len(cpus), 1)

        page = self.page_size
        per_second = float(self.clk_tck)
        mem_total = float(mem["total"] or 1)
        rows = []
        nprocs = nthreads = nkthreads = 0
        before = prev["procs"]
        for key, p in cur["procs"].items():
            (pid, start, ppid, uid, state, priority, nice, nlwp, ticks,
             size, resident, shared, kthread, cmd) = p
            old = before.get(key)
            # A process missing from the previous sample started after it.
            delta = ticks - (old[8] if old is not None else 0)
            cpu = max(delta, 0) / per_second / dt * 100.0
            res = resident * page
            rows.append([pid, ppid, self.user_name(uid), priority, nice,
                         size * page, res, shared * page, state,
                         round(cpu, 1), round(res * 100.0 / mem_total, 1),
                         ticks * 100 // self.clk_tck, nlwp, 1 if kthread else 0,
                         cmd[0], cmd[1], cmd[2]])
            if kthread:
                nkthreads += 1
            else:
                nprocs += 1
                nthreads += max(nlwp - 1, 0)

        return {
            "v": SCHEMA,
            "host": self._host_text("hostname", "localhost"),
            "kernel": self._host_text("osrelease", ""),
            "time": round(cur["wall"], 3),
            "dt": round(dt, 3),
            "ncpu": len(cpus),
            "cpu_fields": CPU_FIELDS,
            "cpu": cpu_percentages(prev["cpu"], cur["cpu"]),
            "cpus": cpus,
            "mem": mem,
            "swap": swap,
            "load": [float(load[0]), float(load[1]), float(load[2])],
            "uptime": uptime,
            "tasks": {"procs": nprocs, "threads": nthreads, "kthreads": nkthreads,
                      "running": min(cur["running"], ncpu)},
            "proc_fields": PROC_FIELDS,
            "procs": rows,
        }


def run(interval=2.0, proc="/proc", once=False, out=None):
    """Print snapshots until the reader goes away. Returns False on a broken pipe."""
    out = out or sys.stdout
    interval = max(float(interval), 0.1)
    sampler = Sampler(proc)
    parent = os.getppid()
    prev = sampler.sample()
    delay = min(interval, 0.5)  # the first snapshot comes quickly
    while True:
        time.sleep(delay)
        cur = sampler.sample()
        line = json.dumps(sampler.snapshot(prev, cur), separators=(",", ":"))
        try:
            out.write(line + "\n")
            out.flush()
        except (BrokenPipeError, ConnectionResetError):
            return False
        if once:
            return True
        if os.getppid() != parent:
            return True  # the SSH session or htop-web is gone
        prev = cur
        delay = max(interval - (time.monotonic() - cur["t"]), 0.05)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Print htop-style JSON snapshots of this Linux host.")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between snapshots (default: 2)")
    parser.add_argument("--proc", default="/proc", help="proc filesystem root")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    args = parser.parse_args(argv)
    try:
        ok = run(args.interval, args.proc, args.once)
    except KeyboardInterrupt:
        ok = True
    if not ok:
        # Keep the interpreter quiet when it flushes the closed pipe on exit.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    return 0


if __name__ == "__main__":
    sys.exit(main())
