"""Synthetic hosts for --demo: plausible numbers with no real machine behind them."""

from __future__ import annotations

import random
import time

from .collector import CPU_FIELDS, PROC_FIELDS, basename_span
from .model import HostSpec

GIB = 1024 ** 3
MIB = 1024 ** 2

PROFILES = {
    "gpu-node-01": {"ncpu": 64, "mem": 256, "swap": 8, "kernel": "6.8.0-49-generic",
                    "days": 41, "seed": 11, "system": 0.10, "steal": 0.0},
    "web-01": {"ncpu": 8, "mem": 16, "swap": 2, "kernel": "6.1.0-27-cloud-amd64",
               "days": 213, "seed": 12, "system": 0.25, "steal": 0.06},
    "db-01": {"ncpu": 16, "mem": 64, "swap": 4, "kernel": "6.8.0-49-generic",
              "days": 87, "seed": 13, "system": 0.20, "steal": 0.0},
    "build-01": {"ncpu": 32, "mem": 128, "swap": 0, "kernel": "6.11.0-9-generic",
                 "days": 3, "seed": 14, "system": 0.12, "steal": 0.0},
}

# (user, command, CPU % of one core, resident MiB, threads, nice[, count])
COMMON = [
    ("root", "/usr/lib/systemd/systemd-journald", 0.1, 48, 1, 0),
    ("root", "/usr/lib/systemd/systemd-udevd", 0.0, 9, 1, 0),
    ("systemd-resolve", "/usr/lib/systemd/systemd-resolved", 0.0, 12, 1, 0),
    ("systemd-timesync", "/usr/lib/systemd/systemd-timesyncd", 0.0, 7, 2, 0),
    ("messagebus", "/usr/bin/dbus-daemon --system --address=systemd: --nofork --nopidfile "
     "--systemd-activation --syslog-only", 0.0, 6, 1, 0),
    ("root", "/usr/sbin/cron -f -P", 0.0, 3, 1, 0),
    ("syslog", "/usr/sbin/rsyslogd -n -iNONE", 0.0, 6, 4, 0),
    ("root", "/usr/sbin/irqbalance", 0.1, 4, 2, 0),
    ("root", "/usr/lib/polkit-1/polkitd --no-debug", 0.0, 9, 3, 0),
    ("root", "sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups", 0.0, 9, 1, 0),
    ("root", "/usr/bin/containerd", 0.4, 52, 18, 0),
    ("prometheus", "/usr/local/bin/node_exporter --collector.systemd", 0.3, 24, 9, 0),
    ("root", "/sbin/agetty -o -p -- \\u --noclear - linux", 0.0, 2, 1, 0),
]

WORKLOADS = {
    "gpu-node-01": [
        ("alice", "/opt/conda/envs/train/bin/python train.py --config configs/vit_large.yaml "
         "--epochs 300", 1450, 18900, 96, 0),
        ("alice", "/opt/conda/envs/train/bin/python train.py --config configs/vit_large.yaml "
         "--epochs 300", 92, 2400, 4, 0, 8),
        ("alice", "/opt/conda/envs/train/bin/python -m torch.distributed.run --nproc-per-node 8 "
         "train.py", 3, 1500, 24, 0),
        ("bob", "/opt/conda/bin/python preprocess.py --workers 16 --input /data/raw "
         "--output /data/shards", 640, 7200, 17, 10),
        ("bob", "/usr/bin/python3 -m jupyterlab --no-browser --port 8888", 2.5, 480, 8, 0),
        ("carol", "tensorboard --logdir runs --port 6006", 3.5, 910, 31, 0),
        ("root", "/usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock",
         1.2, 140, 42, 0),
        ("root", "/usr/bin/rsync --server --sender -logDtpre.iLsfxCIvu . /data/checkpoints/",
         21, 30, 1, 19),
        ("alice", "sshd: alice@pts/0", 0.1, 7, 1, 0),
        ("alice", "-zsh", 0.0, 6, 1, 0),
        ("alice", "tmux new -s train", 0.2, 5, 1, 0),
        ("carol", "htop", 1.5, 5, 1, 0),
    ],
    "web-01": [
        ("root", "nginx: master process /usr/sbin/nginx -g daemon on; master_process on;",
         0.0, 10, 1, 0),
        ("www-data", "nginx: worker process", 2.5, 14, 1, 0, 4),
        ("app", "/srv/app/.venv/bin/python /srv/app/.venv/bin/gunicorn app.wsgi:application "
         "--workers 5 --bind unix:/run/app.sock", 11, 260, 3, 0, 5),
        ("redis", "/usr/bin/redis-server 127.0.0.1:6379", 1.4, 85, 5, 0),
        ("app", "node /srv/web/server.js", 6.0, 210, 11, 0),
        ("app", "/srv/app/.venv/bin/python -m celery -A app worker --concurrency 4", 4.0, 310,
         5, 5),
        ("deploy", "sshd: deploy@notty", 0.0, 6, 1, 0),
    ],
    "db-01": [
        ("postgres", "/usr/lib/postgresql/16/bin/postgres -D /var/lib/postgresql/16/main "
         "-c config_file=/etc/postgresql/16/main/postgresql.conf", 0.5, 180, 1, 0),
        ("postgres", "postgres: 16/main: checkpointer", 0.3, 140, 1, 0),
        ("postgres", "postgres: 16/main: background writer", 0.2, 60, 1, 0),
        ("postgres", "postgres: 16/main: walwriter", 0.4, 30, 1, 0),
        ("postgres", "postgres: 16/main: autovacuum launcher", 0.0, 12, 1, 0),
        ("postgres", "postgres: 16/main: app appdb [local] SELECT", 38, 900, 1, 0, 6),
        ("postgres", "postgres: 16/main: app appdb [local] idle", 0.0, 220, 1, 0, 10),
        ("bob", "pg_dump -Fc -f /backup/appdb.dump appdb", 45, 120, 1, 10),
        ("root", "/usr/sbin/smartd -n", 0.0, 4, 1, 0),
    ],
    "build-01": [
        ("ci", "/usr/bin/make -j32 all", 0.5, 12, 1, 0),
        ("ci", "/usr/lib/gcc/x86_64-linux-gnu/13/cc1plus -quiet -I include -O2 "
         "src/engine/module.cpp -o /tmp/cc0a1b2c.s", 98, 620, 1, 0, 24),
        ("ci", "/usr/bin/ld -plugin /usr/libexec/gcc/x86_64-linux-gnu/13/liblto_plugin.so "
         "-o build/engine", 99, 2300, 1, 0),
        ("ci", "/home/ci/actions-runner/bin/Runner.Listener run --startuptype service", 0.5, 110,
         14, 0),
        ("ci", "/home/ci/actions-runner/bin/Runner.Worker spawnclient 117 120", 1.0, 95, 12, 0),
    ],
}

KTHREADS_PER_CPU = ("cpuhp/{}", "migration/{}", "ksoftirqd/{}", "kworker/{}:0-events",
                    "kworker/{}:1H-kblockd")
KTHREADS = ("rcu_preempt", "kswapd0", "khugepaged", "kcompactd0", "watchdogd",
            "jbd2/nvme0n1p2-8", "kworker/u64:2-flush-259:0")


def demo_specs() -> list[HostSpec]:
    return [HostSpec(key=name, kind="demo") for name in PROFILES]


class DemoHost:
    def __init__(self, name: str):
        profile = PROFILES.get(name, PROFILES["web-01"])
        self.name = name
        self.profile = profile
        self.rng = random.Random(profile["seed"])
        self.ncpu = profile["ncpu"]
        self.mem_total = profile["mem"] * GIB
        self.boot = time.time() - profile["days"] * 86400 - self.rng.uniform(0, 86400)
        self.last = time.monotonic() - 2.0
        self.affinity = [self.rng.uniform(0.55, 1.45) for _ in range(self.ncpu)]
        self.load = None
        self.next_pid = 3
        self.procs: list[dict] = []
        self._spawn("root", "/sbin/init splash", 0.0, 13, 1, 0, pid=1)
        self._spawn("root", "kthreadd", 0.0, 0, 1, 0, kthread=True, pid=2)
        for i in range(self.ncpu):
            for pattern in KTHREADS_PER_CPU:
                self._spawn("root", pattern.format(i), 0.0, 0, 1, 0, kthread=True)
        for comm in KTHREADS:
            self._spawn("root", comm, 0.05, 0, 1, 0, kthread=True)
        self.next_pid = max(self.next_pid + 50, 600)
        for entry in COMMON + WORKLOADS.get(name, []):
            user, cmd, cpu, res, threads, nice = entry[:6]
            for _ in range(entry[6] if len(entry) > 6 else 1):
                self._spawn(user, cmd, cpu, res, threads, nice)
            self.next_pid += self.rng.randint(5, 4000)

    def _spawn(self, user, cmd, cpu, res_mib, threads, nice, kthread=False, pid=None):
        if pid is None:
            pid = self.next_pid
            self.next_pid += self.rng.randint(1, 3)
        if kthread:
            hs, he = 0, len(cmd)
        else:
            hs, he = basename_span(cmd, cmd.split(" ")[0])
        res = int(res_mib * MIB * self.rng.uniform(0.9, 1.1))
        self.procs.append({
            "pid": pid, "user": user, "cmd": cmd, "hs": hs, "he": he, "base": cpu, "cpu": cpu,
            "res": res, "virt": int(res * self.rng.uniform(2.5, 9)) if res else 0,
            "shr": int(res * self.rng.uniform(0.05, 0.3)), "threads": threads, "nice": nice,
            "kthread": kthread, "time": int(cpu * self.rng.uniform(300, 30000)),
        })

    def _step_processes(self, dt: float) -> None:
        rng = self.rng
        for p in self.procs:
            base = p["base"]
            p["cpu"] = max(base * rng.uniform(0.7, 1.25) + rng.uniform(-0.3, 0.3) * (base > 0), 0.0)
            p["time"] += int(p["cpu"] * dt)
        if self.name == "build-01":  # compiler jobs come and go
            for p in self.procs:
                if "cc1plus" in p["cmd"] and rng.random() < 0.2:
                    p["pid"] = self.next_pid
                    p["time"] = 0
                    p["res"] = int(rng.uniform(300, 900) * MIB)
                    self.next_pid += rng.randint(3, 40)

    def snapshot(self) -> dict:
        rng = self.rng
        now = time.monotonic()
        dt = min(max(now - self.last, 0.2), 10.0)
        self.last = now
        self._step_processes(dt)

        demand = sum(p["cpu"] for p in self.procs) / 100.0
        nice_demand = sum(p["cpu"] for p in self.procs if p["nice"] > 0) / 100.0
        level = min(demand / self.ncpu, 0.97)
        nice_share = nice_demand / demand if demand else 0.0
        cpus, totals = [], [0.0] * len(CPU_FIELDS)
        for i in range(self.ncpu):
            busy = min(max(level * self.affinity[i] + rng.uniform(-0.08, 0.08), 0.005), 0.995) * 100
            system = busy * self.profile["system"] * rng.uniform(0.6, 1.4)
            steal = busy * self.profile["steal"]
            nice = (busy - system - steal) * nice_share
            user = max(busy - system - steal - nice, 0.0)
            values = [round(user, 2), round(nice, 2), round(system, 2), 0.3, 0.4,
                      round(rng.uniform(0, 1.5), 2), round(steal, 2), 0.0]
            cpus.append([i] + values)
            totals = [a + b for a, b in zip(totals, values)]
        cpu = [round(v / self.ncpu, 2) for v in totals]

        load_now = demand * rng.uniform(0.95, 1.08)
        self.load = self.load or [load_now * 0.97, load_now * 0.92, load_now * 0.9]
        for k, weight in enumerate((0.3, 0.08, 0.03)):
            self.load[k] += (load_now - self.load[k]) * weight

        res_total = sum(p["res"] for p in self.procs)
        used = min(int(res_total * 1.15 + self.mem_total * 0.04), int(self.mem_total * 0.92))
        buffers = int(self.mem_total * 0.012)
        shared = int(self.mem_total * 0.018)
        cache = int((self.mem_total - used - buffers - shared) * 0.55)
        swap_total = self.profile["swap"] * GIB
        swap_used = int(swap_total * 0.07)

        rows, nprocs, nthreads, nkthreads, running = [], 0, 0, 0, 0
        for p in self.procs:
            if p["kthread"]:
                state = "I" if p["cpu"] < 0.5 else "S"
                pri = -100 if p["cmd"].startswith("migration/") else 20
                nkthreads += 1
            else:
                state = "R" if p["cpu"] > 60 or rng.random() < p["cpu"] / 200 else "S"
                if p["cmd"].startswith("pg_dump") and rng.random() < 0.5:
                    state = "D"
                pri = 20 + p["nice"]
                nprocs += 1
                nthreads += p["threads"] - 1
            running += state == "R"
            rows.append([p["pid"], 1, p["user"], pri, p["nice"], p["virt"], p["res"], p["shr"],
                         state, round(p["cpu"], 1), round(p["res"] * 100 / self.mem_total, 1),
                         p["time"], p["threads"], 1 if p["kthread"] else 0, p["cmd"], p["hs"],
                         p["he"]])

        return {
            "v": 1, "host": self.name, "kernel": self.profile["kernel"], "time": time.time(),
            "dt": round(dt, 3), "ncpu": self.ncpu, "cpu_fields": CPU_FIELDS, "cpu": cpu,
            "cpus": cpus,
            "mem": {"total": self.mem_total, "used": used, "buffers": buffers, "shared": shared,
                    "cache": cache, "available": self.mem_total - used - shared,
                    "free": self.mem_total - used - buffers - shared - cache},
            "swap": {"total": swap_total, "used": swap_used, "cache": swap_used // 10},
            "load": [round(v, 2) for v in self.load], "uptime": time.time() - self.boot,
            "tasks": {"procs": nprocs, "threads": nthreads, "kthreads": nkthreads,
                      "running": min(running, self.ncpu)},
            "proc_fields": PROC_FIELDS, "procs": rows,
        }
