#!/usr/bin/env python3
"""wrk on Linux against a static nginx: the Linux arm of the HTTP load
comparison with apps/bench/smoltcp-s3 run with server=<type>. Same server
binary and blob, same block size and per-block connection policy; one row per
run, with wrk's request rate and latency percentiles beside the wire rate and
the client cpu the run consumed.

    just bench competitors/wrk-http --sweep conns=64,128
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import runner  # noqa: E402
from httpserver import HttpServer  # noqa: E402
from runner import ROOT, size  # noqa: E402

# The Linux launch path (AMI, user-data, console, terminate) is linux-s3's.
_spec = importlib.util.spec_from_file_location("linux_s3_bench", HERE / "linux-s3" / "bench.py")
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(_mod)
LinuxS3 = _mod.LinuxS3

BENCH = "competitors/wrk-http"


class WrkHttp(LinuxS3):
    name = "wrk-http"
    os_name = "linux"
    bench_path = BENCH
    scripts_dir = ROOT / BENCH / "scripts"
    knobs = {
        "threads": ("BENCH_THREADS", int),
        "conns": ("BENCH_CONNS", int),  # total, spread over the threads
        "duration": ("BENCH_DURATION", int),
        "block": ("BENCH_BLOCK_SIZE", size),
        # "1": Connection: close on every request, the smoltcp-s3 arm's shape.
        "close": ("BENCH_CLOSE", str),
        # The nginx instance type; launched once per point.
        "server": ("BENCH_SERVER", str),
    }
    defaults = {"threads": 8, "conns": 128, "duration": 30, "block": 128 << 20, "close": "1", "server": "c6in.8xlarge"}
    instance_tag = "miniosv-wrk-bench"
    default_instance = "c6in.8xlarge"
    max_vm_seconds = 600
    metrics = {
        "gbps": (r"^AGGREGATE: \d+ requests, ([\d.]+) Gbps", float),
        "requests": (r"^AGGREGATE: (\d+) requests", int),
        "rps": (r"^Requests/sec:\s+([\d.]+)", float),
        "lat_avg_ms": (r"^LATENCY: .*\bavg_ms=([\d.]+)", float),
        "lat_p50_ms": (r"^LATENCY: .*\bp50_ms=([\d.]+)", float),
        "lat_p90_ms": (r"^LATENCY: .*\bp90_ms=([\d.]+)", float),
        "lat_p99_ms": (r"^LATENCY: .*\bp99_ms=([\d.]+)", float),
        "lat_max_ms": (r"^LATENCY: .*\bmax_ms=([\d.]+)", float),
        "gbps_wire": (r"^RX: .* => ([\d.]+) Gbps on the wire", float),
        "rx_packets": (r"^RX: \d+ bytes, (\d+) packets", int),
        "cpu_busy": (r"^CPU: busy=([\d.]+)", float),
        "cpu_cores": (r"^CPU: busy=[\d.]+ cores=([\d.]+)", float),
        "non2xx": (r"^\s+Non-2xx or 3xx responses: (\d+)", int),
        "sock_err_connect": (r"^\s+Socket errors: connect (\d+)", int),
        "sock_err_read": (r"^\s+Socket errors: connect \d+, read (\d+)", int),
        "sock_err_timeout": (r"^\s+Socket errors: .*timeout (\d+)", int),
        "instance_type": (r"^instance     : (\S+)", str),
        "kernel": (r"^kernel       : (\S+)", str),
        "nproc": (r"^nproc        : (\d+)", int),
        "scheme": (r"^target       : (http)://", str),
    }

    def __init__(self) -> None:
        super().__init__()
        self.server: HttpServer | None = None

    def build(self, cfg: dict, ip: str) -> None:
        super().build(cfg, ip)  # just setup competitors/wrk-http
        if self.server:
            self.server.stop()
        self.server = HttpServer(str(cfg["server"]), os.environ.get("BENCH_SERVER_MARKET", "on-demand"),
                                               ROOT / "results/http/logs", client=self.instance,
                                 size=os.environ.get("AWS_BUCKET_SIZE", "10G"))
        self.server.start()
        self.zone = self.server.zone  # same zone: no cross-AZ hop, no cross-AZ bill

    def user_data(self, cfg: dict, ip: str, run_id: str) -> str:
        import json
        assert self.server and self.server.ip
        conf = {
            "AWS_BUCKET": os.environ["AWS_BUCKET"],
            "AWS_REGION": os.environ["AWS_REGION"],
            "AWS_TARGET_IP": self.server.ip,
            "BENCH_THREADS": str(cfg["threads"]),
            "BENCH_CONNS": str(cfg["conns"]),
            "BENCH_DURATION": str(cfg["duration"]),
            "BENCH_BLOCK_SIZE": str(cfg["block"]),
            "BENCH_OBJECT_SIZE": str(size(os.environ.get("AWS_BUCKET_SIZE", "10G"))),
            "BENCH_CLOSE": str(cfg["close"]),
            "RUN_ID": run_id,
        }
        body = (self.scripts_dir / "instance.py").read_text()
        body = body.split("\n", 1)[1] if body.startswith("#!") else body
        return "#!/usr/bin/env python3\nCONFIG = {}\n{}".format(json.dumps(conf), body)

    def run_once(self, instance: str, logdir: Path, cfg: dict, ip: str) -> dict:
        assert self.server and self.server.ip
        row = super().run_once(instance, logdir, cfg, self.server.ip)
        row["server_id"], row["server_zone"], row["target_ip"] = self.server.iid, self.server.zone, self.server.ip
        return row

    def valid(self, row: dict) -> bool:
        return bool(row.get("complete")) and not any(
            row.get(k) for k in ("non2xx", "sock_err_connect", "sock_err_read", "sock_err_timeout"))

    def summary(self, row: dict) -> str:
        return (f"{row.get('gbps')} Gbps, {row.get('rps')} req/s, p50 {row.get('lat_p50_ms')} ms, "
                f"p99 {row.get('lat_p99_ms')} ms, {row.get('cpu_cores')} cores busy")


def main() -> None:
    bench = WrkHttp()
    if "--dry-run" not in sys.argv[1:]:
        bench.ami = bench.resolve_ami(None)
        print(f"ami      : {bench.ami}")
    runner.cli(bench)


if __name__ == "__main__":
    main()
