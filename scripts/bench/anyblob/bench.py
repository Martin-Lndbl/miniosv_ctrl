#!/usr/bin/env python3
"""Sweep AnyBlob (Durner, Leis, Neumann, VLDB 2023) on a stock AL2023 AMI;
same CSV shape and validity gate as the smoltcp and Linux sweeps, so the three
stacks' AGGREGATE numbers sit in one plot.

    just bench competitors/anyblob --sweep conns=16,24,32 --workers 16 --blocks 96
    just bench competitors/anyblob --sweep block=16M,128M --workers 12 --conns 16

Runtime knobs, one static binary, no `just deploy`: the launch path is
competitors/linux-s3's, imported from its driver. What differs is the binary
in the bucket, the instance script, and the knobs: `pin` says whether the S3
name is pinned to the given front-end (through /etc/hosts) or resolved the
way AnyBlob's throughput-based resolver would in production, the default.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpserver  # noqa: E402
import runner  # noqa: E402
from httpserver import HttpServer  # noqa: E402
from runner import ROOT, size  # noqa: E402

# scripts/bench/linux-s3 is not an importable name; load it by path.
_spec = importlib.util.spec_from_file_location(
    "linux_s3_bench", Path(__file__).resolve().parents[1] / "linux-s3" / "bench.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
LinuxS3 = _mod.LinuxS3

BENCH = "competitors/anyblob"


def flag(v) -> int:
    return 1 if str(v).strip().lower() in ("1", "true", "yes", "pinned", "pin") else 0


class AnyBlob(LinuxS3):
    name = "anyblob"
    os_name = "anyblob"
    bench_path = BENCH
    scripts_dir = ROOT / BENCH / "scripts"
    knobs = {
        "workers": ("BENCH_WORKERS", size),          # AnyBlob daemons (threads)
        "conns": ("BENCH_CONNS_PER_WORKER", size),    # requests in flight per daemon, <= 128
        "block": ("BENCH_BLOCK_SIZE", size),
        "blocks": ("BENCH_BLOCKS", size),             # per worker; 0 = conns
        "chunk": ("BENCH_CHUNK", size),               # recv size per io_uring op
        # 1 = /etc/hosts pins the bucket to the target IP; 0 = resolve normally.
        "pin": ("BENCH_PIN", flag),
        # The nginx arm. `url` points the library at any http(s)://host/key
        # instead of the bucket -- main.cpp already takes it, through
        # Provider::makeAnonymousProvider, and it is how AnyBlob joins the
        # bandwidth figure. Left empty the sweep is the S3 one it always was.
        "scheme": ("BENCH_SCHEME", str),
        "url": ("BENCH_URL", str),
        # 0 = the machine as shipped; N confines the daemons and the NIC to N
        # cores, by the same code competitors/linux-http uses.
        "cpus": ("BENCH_CPUS", int),
        # stock = AL2023 as shipped; nogro = mininet's wire (GRO off, mtu 9001)
        "mode": ("MODE", str),
        # The nginx instance type; launched once per point. "" dials a
        # long-living one through --target-ip.
        "server": ("BENCH_SERVER", str),
    }
    defaults = {"workers": 0, "conns": 24, "block": 128 << 20, "blocks": 0,
                "chunk": 64 << 10, "pin": 0, "scheme": "https", "url": "",
                "cpus": 0, "mode": "stock", "server": ""}
    instance_tag = "anyblob-bench"
    default_instance = "c6in.16xlarge"
    max_vm_seconds = 900

    metrics = LinuxS3.metrics | {
        "workers_actual": (r"^bench: (\d+) workers", int),
        "http_bad": (r"^http status\s+: (\d+) non-206", int),
        "hdr_bad": (r"^response heads: (\d+) did not match", int),
        "tail_ms_max": (r"^TAIL STATS\s*: idle_max_ms=([\d.]+)", float),
        "tail_ms_avg": (r"^TAIL STATS\s*: .*idle_avg_ms=([\d.]+)", float),
        "ttfb_us_avg": (r"^REQ STATS\s*: .*ttfb_us_avg=(\d+)", int),
        "wire_us_avg": (r"^REQ STATS\s*: .*wire_us_avg=(\d+)", int),
        "wire_us_p50": (r"^REQ STATS\s*: .*wire_us_p50=(\d+)", int),
        "wire_us_p99": (r"^REQ STATS\s*: .*wire_us_p99=(\d+)", int),
        "cpu_user_s": (r"^CPU STATS\s*: user_s=([\d.]+)", float),
        "cpu_sys_s": (r"^CPU STATS\s*: .*sys_s=([\d.]+)", float),
        "cpu_total_s": (r"^CPU STATS\s*: .*total_s=([\d.]+)", float),
        "cores_avg": (r"^CPU STATS\s*: .*cores_avg=([\d.]+)", float),
        "pool_s": (r"^pool: .* ([\d.]+) s to allocate", float),
        # From the instance script's counter delta: the dials the library made.
        "tcp_active_opens": (r"^Tcp:ActiveOpens\s+\+(\d+)", int),
        "tcp_retrans": (r"^Tcp:RetransSegs\s+\+(\d+)", int),
        "pinned": (r"^pinned\s+: (\S+)", str),
        # Read back off /proc/stat, so unlike cores_avg (getrusage, process
        # threads only) these include io-wq workers and the softirq receive
        # path. cores_out > 0 means the core budget leaked.
        "cores_in": (r"^CPU BUDGET: cores_in=([\d.]+)", float),
        "cores_out": (r"^CPU BUDGET: .*cores_out=([\d.]+)", float),
    }

    def __init__(self) -> None:
        super().__init__()
        self.server: HttpServer | None = None

    def build(self, cfg: dict, ip: str) -> None:
        super().build(cfg, ip)
        if not cfg.get("url"):
            return  # the S3 sweep: no server of ours to stand up
        if not cfg.get("server"):
            httpserver.verify_external(ip, os.environ.get("BENCH_ZONE"))
            self.zone = os.environ.get("BENCH_ZONE") or None
            return
        if self.server:
            self.server.stop()
        self.server = HttpServer(str(cfg["server"]), os.environ.get("BENCH_SERVER_MARKET", "spot"),
                                 ROOT / "results/http/logs", client=self.instance,
                                 size=os.environ.get("AWS_BUCKET_SIZE", "10G"))
        self.server.start()
        self.zone = self.server.zone  # same zone: no cross-AZ hop, no cross-AZ bill

    def target_ip(self, cfg: dict, ip: str) -> str:
        return self.server.ip if self.server else ip

    def run_once(self, instance: str, logdir, cfg: dict, ip: str) -> dict:
        target = self.target_ip(cfg, ip)
        row = super().run_once(instance, logdir, cfg, target)
        if self.server:
            row["server_id"], row["server_zone"], row["target_ip"] = self.server.iid, self.server.zone, self.server.ip
        elif cfg.get("url"):
            row["server_zone"], row["target_ip"] = os.environ.get("BENCH_ZONE", ""), target
        return row

    def user_data(self, cfg: dict, ip: str, run_id: str) -> str:
        import json
        target = self.target_ip(cfg, ip)
        # `url` is a template: the address is only known once the server is up,
        # so "http://HOST/blob.bin" in the TOML becomes the private IP here.
        url = str(cfg.get("url") or "").replace("HOST", target)
        conf = {
            "AWS_BUCKET": os.environ["AWS_BUCKET"],
            "AWS_REGION": os.environ["AWS_REGION"],
            "AWS_BUCKET_SIZE": os.environ.get("AWS_BUCKET_SIZE", "10G"),
            "AWS_TARGET_IP": target,
            # One daemon per core of the budget, so the axis moves the same
            # thing it moves on the unikernel arm, where a worker owns a core.
            "BENCH_WORKERS": str(cfg["workers"] or cfg["cpus"] or 16),
            "BENCH_CONNS_PER_WORKER": str(cfg["conns"]),
            "BENCH_BLOCK_SIZE": str(cfg["block"]),
            "BENCH_BLOCKS": str(cfg["blocks"]),
            "BENCH_CHUNK": str(cfg["chunk"]),
            "BENCH_PIN": str(cfg["pin"]),
            "BENCH_SCHEME": str(cfg["scheme"]),
            "BENCH_URL": url,
            "BENCH_CPUS": str(cfg["cpus"]),
            "MODE": str(cfg["mode"]),
            "RUN_ID": run_id,
        }
        return self.guest_script(conf)

    def valid(self, row: dict) -> bool:
        # The shared gate, plus the two checks the library's callback makes.
        return bool(
            super().valid(row)
            and (row.get("hdr_bad") or 0) == 0
        )

    def summary(self, row: dict) -> str:
        s = (
            f"{row.get('gbps')} Gbps, {row.get('workers_actual')} workers, "
            f"{row.get('conns_clean')}/{row.get('conns_total')} requests ok"
        )
        if row.get("cores_avg") is not None:
            s += f", {row['cores_avg']} cores"
        if row.get("gbps_wire_steady") is not None:
            s += f", wire steady {row['gbps_wire_steady']}"
        return s


def main() -> None:
    bench = AnyBlob()
    argv = sys.argv[1:]
    if "--dry-run" not in argv:
        override = None
        if "--ami" in argv:
            override = argv[argv.index("--ami") + 1]
        bench.ami = bench.resolve_ami(override)
        print(f"ami      : {bench.ami}")
    runner.cli(bench)


if __name__ == "__main__":
    main()
