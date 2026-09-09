#!/usr/bin/env python3
"""Sweep TPC-H queries against DuckDB-on-miniOSv, reading parquet over
mininet+httpfs from the bucket `just setup apps/bench/duckdb-tpch` filled.

    just bench apps/bench/duckdb-tpch --sweep query=1,3,6,10,12
    just bench apps/bench/duckdb-tpch --sweep query=1,2,3,...,22 --reps 1
    just bench apps/bench/duckdb-tpch --sweep sf=1,0.1 --query 6

Unlike smoltcp-s3, the axis here (which query, which scale factor) is a boot
argument, not a compiled-in knob -- `main.cc`'s `tpch` executable takes it at
runtime. `workers`/`conns` are still compile-time (MININET_WORKERS/CONNS), so
sweeping those still rebuilds; sweeping `query` or `sf` just rewrites the
boot-args sector (scripts/setargs.py) and reuses the same image, which `make`
confirms is up to date in under a second.

Sweep machinery is in ../runner.py, shared with smoltcp-s3 and linux-s3 --
this driver only differs in what it builds, runs, and parses.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runner  # noqa: E402
from runner import ROOT, Bench, ec2, parse  # noqa: E402

MINIOSV = ROOT / "miniosv"
IMAGE = MINIOSV / "build/release.x64/loader.img"


class DuckdbTpch(Bench):
    name = "duckdb-tpch"
    os_name = "miniosv"
    knobs = {
        "query": (None, int),  # boot arg -- which TPC-H query (1-22)
        "sf": (None, str),  # boot arg -- scale factor, matches tpch/sf<N>/
        "workers": ("MININET_WORKERS", int),  # compiled in
        "conns": ("MININET_CONNS", int),  # compiled in
        "tls": ("MININET_TLS", int),  # compiled in -- 0 dials plain HTTP:80
    }
    defaults = {"query": 6, "sf": "1", "workers": 2, "conns": 8, "tls": 1}
    instance_tag = "miniosv-loader-*"  # aws-deploy.py names every image this
    default_instance = "c7i.large"  # correctness + latency, not a throughput sweep
    max_vm_seconds = 300  # lineitem is 197 MiB at sf=1, no connection reuse yet (M3)
    headline_metric = "query_ms"
    headline_agg = "min"
    headline_unit = "ms"

    metrics = {
        "scheme": (r"^tpch: sf=[\d.]+, \d+ quer\w+, bucket \S+ \((\w+)\)", str),
        "query_ms": (r"^Q\d+: ([\d.]+) ms,", float),
        "rows": (r"^Q\d+: [\d.]+ ms, (\d+) rows", int),
        "match": (r"^Q\d+: [\d.]+ ms, \d+ rows, match=(\w+)", str),
        "queries_ok": (r"^TPCH SUMMARY: ok=(\d+)", int),
        "queries_total": (r"^TPCH SUMMARY: ok=\d+ total=(\d+)", int),
        "checked": (r"checked=(\d+)", int),
        "matched": (r"matched=(\d+)", int),
    }

    def summary(self, row: dict) -> str:
        return (
            f"Q{row.get('query')}: {row.get('query_ms')} ms, "
            f"{row.get('rows')} rows, match={row.get('match')}"
        )

    def build(self, cfg: dict, ip: str) -> None:
        """Rebuild only when workers/conns changed (main.o depends on a stamp
        file miniosv.mk only touches when MININET_* actually moved); query/sf
        always get a fresh boot-args sector, which costs nothing to redo."""
        bucket = os.environ["AWS_BUCKET"]
        region = os.environ["AWS_REGION"]
        host = f"{bucket}.s3.{region}.amazonaws.com"
        env = {
            **os.environ,
            "MININET_HOST": host,
            "MININET_ADDR": ip,
            "MININET_WORKERS": str(cfg["workers"]),
            "MININET_CONNS": str(cfg["conns"]),
            "MININET_TLS": str(cfg["tls"]),
        }
        # Not `just build`: that coerces its `app` argument through
        # absolute_path(), which breaks the `app=duckdb` shorthand app/Makefile
        # relies on to find app/miniduckdb/miniosv/miniosv.mk.
        r = subprocess.run(
            ["make", "-C", str(MINIOSV), "app=duckdb", f"-j{os.cpu_count()}"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        if r.returncode:
            raise SystemExit(f"build failed for {cfg}:\n{r.stdout}\n{r.stderr}")

        args = f"tpch --sf {cfg['sf']} {cfg['query']}"
        r = subprocess.run(
            [sys.executable, str(MINIOSV / "scripts/setargs.py"), str(IMAGE), args],
            capture_output=True,
            text=True,
        )
        if r.returncode:
            raise SystemExit(f"setargs failed for {cfg!r}:\n{r.stdout}\n{r.stderr}")

    def run_once(self, instance: str, logdir: Path, cfg: dict, ip: str) -> dict:
        """Deploy, wait for the guest's verdict, terminate, parse. Mirrors
        smoltcp-s3's run_once: termination is an API call because a signal can
        resolve to the driver's own pgid and orphan a billing instance."""
        logdir.mkdir(parents=True, exist_ok=True)
        log = logdir / f"deploy-{instance}-{int(time.time())}.log"
        if up := self.live():
            print(
                f"    note: {len(up)} other {self.instance_tag} instance(s) "
                f"up, not ours: {', '.join(up)}",
                flush=True,
            )

        with log.open("w") as fh:
            p = subprocess.Popen(
                ["just", "deploy", instance],
                cwd=ROOT,
                stdout=fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            iid = None
            for _ in range(1500):  # wait for the instance to exist
                if m := re.search(
                    r"Instance running: (i-[0-9a-f]+)", log.read_text(errors="replace")
                ):
                    iid = m.group(1)
                    break
                if p.poll() is not None:
                    break
                time.sleep(1)
            if iid:  # billing starts here: cap it
                for _ in range(self.max_vm_seconds):
                    if re.search(
                        r"^(COMPLETE|INCOMPLETE):",
                        log.read_text(errors="replace"),
                        re.M,
                    ):
                        break
                    if p.poll() is not None:
                        break
                    time.sleep(1)
                ec2().terminate_instances(InstanceIds=[iid])
            # SIGINT runs aws-deploy.py's teardown. To the GROUP, not p: p is
            # `just`, which does not forward it.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(p.pid), signal.SIGINT)
            try:
                p.wait(timeout=75)
            except subprocess.TimeoutExpired:
                print(
                    "WARN: aws-deploy.py did not finish its teardown in 75s; "
                    "killing it — check for a leaked AMI and snapshot",
                    flush=True,
                )
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(p.pid), 9)

        if iid is None:
            print(
                "WARN: no instance id in the deploy log — if aws-deploy.py "
                "launched one, check for it by hand",
                flush=True,
            )

        text = log.read_text(errors="replace")
        row = parse(text, self.metrics)
        row["complete"] = bool(re.search(r"^COMPLETE:", text, re.M))
        row["log"] = log.name
        return row

    def valid(self, row: dict) -> bool:
        """No conns/syn_retries here (that is the network stack's own gate,
        exercised by smoltcp-s3); this bench's gate is the guest's own verdict
        plus, when the scale factor had a canned answer, that it matched."""
        return bool(row.get("complete")) and row.get("match") != "no"


if __name__ == "__main__":
    runner.cli(DuckdbTpch())
