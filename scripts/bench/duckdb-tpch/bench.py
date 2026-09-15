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

import boto3  # noqa: E402
import runner  # noqa: E402
from runner import ROOT, Bench, ec2, parse  # noqa: E402

MINIOSV = ROOT / "miniosv"
# How a miniOSv guest reports that it is dead. Any of these means no verdict is
# ever coming, so the instance should be terminated rather than waited out.
CRASH = re.compile(
    r"^(?:page fault outside application.*|Assertion failed:.*|Aborted|\[backtrace\])$",
    re.M,
)
IMAGE = MINIOSV / "build/release.x64/loader.img"
# probe_steps[] in app/miniduckdb/miniosv/main.cc, and PROBE_STEPS in
# competitors/duckdb-linux/scripts/instance.py. All three move together.
PROBE_STEP_NAMES = ("dbgen", "range_scan", "hash_agg", "par_t1",
                    "par_tall", "q01_local", "q06_local")


class DuckdbTpch(Bench):
    name = "duckdb-tpch"
    os_name = "miniosv"
    knobs = {
        "query": (None, int),  # boot arg -- which TPC-H query (1-22)
        "sf": (None, str),  # boot arg -- scale factor, matches tpch/sf<N>/
        # boot arg -- DuckDB's thread count; 0 leaves its own default (one per
        # CPU). Worth sweeping because mininet's workers poll without
        # yielding, so workers + threads can oversubscribe a small instance.
        "threads": (None, int),
        # boot arg -- DuckDB's memory_limit. It otherwise sizes itself from
        # sysconf(_SC_PHYS_PAGES), which reports the whole machine, and grows
        # until the guest's frame allocator is under pressure. "" is DuckDB's
        # own default.
        "memlimit": (None, str),
        # boot arg -- "1" turns on DuckDB's own HTTP request log and adds an
        # `HTTP STATS:` line per query. Diagnostic, and it changes the run it
        # measures: a formatted log record per request is not free, so these
        # rows are comparable to competitors/duckdb-linux's `httplog` pass and
        # to nothing else.
        "httplog": (None, str),
        # boot arg -- "1" boots the `cpuprobe` executable instead of `tpch`:
        # the same ladder competitors/duckdb-linux runs under BENCH_CPU_PROBE,
        # with no network in it at all.
        "cpuprobe": (None, str),
        "workers": ("MININET_WORKERS", int),  # compiled in
        "conns": ("MININET_CONNS", int),  # compiled in
        "tls": ("MININET_TLS", int),  # compiled in -- 0 dials plain HTTP:80
    }
    defaults = {
        "query": 6,
        "sf": "1",
        "threads": 0,
        "memlimit": "",
        "httplog": "",
        "cpuprobe": "",
        "workers": 2,
        "conns": 8,
        "tls": 1,
    }
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
        # Time DuckDB's threads spent blocked in mininet::get(), summed over
        # threads -- so it can exceed wall time, and does when every thread is
        # waiting at once.
        "net_ms": (r"^Q\d+: .*net_ms=([\d.]+)", float),
        "net_calls": (r"^Q\d+: .*net_calls=(\d+)", int),
        "memory_limit": (r"^memory: limit=(\S+)", str),
        "buf_cap_max": (r"^BUF STATS: cap_max=(\d+)", int),
        "requests_retried": (r"^BUF STATS: .*retried=(\d+)", int),
        "hw_concurrency": (r"^cpus: hw_concurrency=(\d+)", int),
        "duckdb_threads": (r"^cpus: .*duckdb_threads=(\d+)", int),
        "requests": (r"^CONN STATS: requests=(\d+)", int),
        "reused": (r"^CONN STATS: requests=\d+ reused=(\d+)", int),
        # The split that says whether latency is queueing for a slot or time
        # on the wire, and whether the workers were on a CPU while it elapsed.
        "queue_us_avg": (r"^REQ STATS: queue_us_avg=(\d+)", int),
        "wire_us_avg": (r"^REQ STATS: .*wire_us_avg=(\d+)", int),
        "body_bytes": (r"^REQ STATS: .*bytes=(\d+)", int),
        "mb_per_s": (r"^REQ STATS: .*mb_per_s=([\d.]+)", float),
        "poll_iters": (r"^POLL STATS: iters=(\d+)", int),
        "poll_gap_us_avg": (r"^POLL STATS: .*gap_us_avg=([\d.]+)", float),
        "poll_gap_us_max": (r"^POLL STATS: .*gap_us_max=(\d+)", int),
        "poll_gaps_over_1ms": (r"^POLL STATS: .*gaps_over_1ms=(\d+)", int),
        # SYN to Established is one round trip, so setup_us_avg is the measured
        # RTT -- the divisor in any window-limited throughput estimate.
        "conns_established": (r"^SETUP STATS: conns=(\d+)", int),
        "conns_failed": (r"^SETUP STATS: .*failed=(\d+)", int),
        "syn_retries": (r"^SETUP STATS: .*syn_retries=(\d+)", int),
        "setup_us_avg": (r"^SETUP STATS: .*setup_us_avg=(\d+)", int),
        # drain_avg near one MSS means the peer sends a segment and waits;
        # tx_ns_avg near the RTT would mean our own ACK is what paces it.
        "recv_drains": (r"^RECV STATS: drains=(\d+)", int),
        "recv_drain_avg": (r"^RECV STATS: .*drain_avg=(\d+)", int),
        "recv_queue_max": (r"^RECV STATS: .*queue_max=(\d+)", int),
        "tx_calls": (r"^TX STATS: calls=(\d+)", int),
        "tx_ns_avg": (r"^TX STATS: .*tx_ns_avg=(\d+)", int),
        "tx_ns_max": (r"^TX STATS: .*tx_ns_max=(\d+)", int),
        # ttfb is S3 think time plus a round trip; xfer is the actual
        # transfer. Which dominates decides whether bandwidth matters at all.
        # Worker publishes a result -> submitting thread running again. The
        # only part of a request that neither wire nor ttfb+xfer covers.
        "wake_n": (r"^WAKE STATS: n=(\d+)", int),
        "wake_ns_avg": (r"^WAKE STATS: .*\bns_avg=(\d+)", int),
        "wake_us_max": (r"^WAKE STATS: .*\bus_max=([\d.]+)", float),
        "wake_ms_total": (r"^WAKE STATS: .*\bms_total=([\d.]+)", float),
        "ttfb_us_avg": (r"^LATENCY STATS: ttfb_us_avg=(\d+)", int),
        "xfer_us_avg": (r"^LATENCY STATS: .*xfer_us_avg=(\d+)", int),
        # Frames lost below smoltcp. imissed is the device dropping for want of
        # a descriptor; the peer reads that as congestion and backs off.
        "imissed": (r"^DROP STATS: imissed=(\d+)", int),
        "ierrors": (r"^DROP STATS: .*ierrors=(\d+)", int),
        "rx_nombuf": (r"^DROP STATS: .*rx_nombuf=(\d+)", int),
        "misrouted": (r"^DROP STATS: .*misrouted=(\d+)", int),
        "tx_alloc_fail": (r"^DROP STATS: .*tx_alloc_fail=(\d+)", int),
        "tx_burst_fail": (r"^DROP STATS: .*tx_burst_fail=(\d+)", int),
        "nic_ipackets": (r"^DROP STATS: .*ipackets=(\d+)", int),
        "nic_ibytes": (r"^DROP STATS: .*ibytes=(\d+)", int),
        # From the httplog pass, and named to match the Linux arm's columns
        # exactly. `http_concurrency` is ms_sum/window_ms -- the mean number
        # of requests in flight, which is what a latency-bound query turns on.
        "http_n": (r"^HTTP STATS: .*\bn=(\d+)", int),
        "http_get": (r"^HTTP STATS: .*\bget=(\d+)", int),
        "http_head": (r"^HTTP STATS: .*\bhead=(\d+)", int),
        "http_ms_avg": (r"^HTTP STATS: .*\bms_avg=([\d.]+)", float),
        "http_ms_p50": (r"^HTTP STATS: .*\bms_p50=([\d.]+)", float),
        "http_ms_p90": (r"^HTTP STATS: .*\bms_p90=([\d.]+)", float),
        "http_ms_p99": (r"^HTTP STATS: .*\bms_p99=([\d.]+)", float),
        "http_ms_max": (r"^HTTP STATS: .*\bms_max=([\d.]+)", float),
        "http_ms_sum": (r"^HTTP STATS: .*\bms_sum=([\d.]+)", float),
        "http_window_ms": (r"^HTTP STATS: .*\bwindow_ms=([\d.]+)", float),
        "http_concurrency": (r"^HTTP STATS: .*\bconcurrency=([\d.]+)", float),
        "http_failed": (r"^HTTP STATS: (FAILED)", str),
        # Broadcast TLB invalidation as this run actually paid for it. A
        # shootdown is one global mutex plus a wait on every other cpu's IPI,
        # so ms_total is directly comparable to the thread-time a query spends
        # outside its reads.
        "tlb_shootdowns": (r"^TLB STATS: shootdowns=(\d+)", int),
        "tlb_pages": (r"^TLB STATS: .*\bpages=(\d+)", int),
        "tlb_all": (r"^TLB STATS: .*\ball=(\d+)", int),
        "tlb_ms_total": (r"^TLB STATS: .*\bms_total=([\d.]+)", float),
        "tlb_us_max": (r"^TLB STATS: .*\bus_max=([\d.]+)", float),
        "tlb_us_avg": (r"^TLB STATS: .*\bus_avg=([\d.]+)", float),
        # Demand paging, the other half of the memory bill. Concurrent across
        # cpus, so ms_total is thread-time and can exceed the wall clock.
        "fault_count": (r"^FAULT STATS: faults=(\d+)", int),
        "fault_sigsegv": (r"^FAULT STATS: .*\bsigsegv=(\d+)", int),
        "fault_ms_total": (r"^FAULT STATS: .*\bms_total=([\d.]+)", float),
        "fault_us_max": (r"^FAULT STATS: .*\bus_max=([\d.]+)", float),
        "fault_ns_avg": (r"^FAULT STATS: .*\bns_avg=([\d.]+)", float),
        # From the cpuprobe ladder. Named per step so one CSV row holds the
        # whole ladder and the two arms' rows subtract column by column.
        "probe_range_scan_ms": (r"^PROBE: name=range_scan ms=([\d.]+)", float),
        "probe_hash_agg_ms": (r"^PROBE: name=hash_agg ms=([\d.]+)", float),
        "probe_dbgen_ms": (r"^PROBE: name=dbgen ms=([\d.]+)", float),
        "probe_par_t1_ms": (r"^PROBE: name=par_t1 ms=([\d.]+)", float),
        "probe_par_tall_ms": (r"^PROBE: name=par_tall ms=([\d.]+)", float),
        "probe_q01_local_ms": (r"^PROBE: name=q01_local ms=([\d.]+)", float),
        "probe_q06_local_ms": (r"^PROBE: name=q06_local ms=([\d.]+)", float),
        # How many cpus carried each ladder step, from their idle threads.
        # parallelism is (cpus x wall - idle) / wall, the same quantity the
        # Linux arm reports as user_ms/real.
        "cpus_par_t1": (r"^CPUS: name=par_t1 .*parallelism=([\d.]+)", float),
        "cpus_par_tall": (r"^CPUS: name=par_tall .*parallelism=([\d.]+)", float),
        "cpus_busy_par_tall": (r"^CPUS: name=par_tall busy=(\d+)", int),
        "cpus_q01_local": (r"^CPUS: name=q01_local .*parallelism=([\d.]+)", float),
        "cpus_dbgen": (r"^CPUS: name=dbgen .*parallelism=([\d.]+)", float),
        "probe_failed": (r"^PROBE: name=\S+ (FAILED)", str),
        "cpuprobe_ok": (r"^(?:IN)?COMPLETE: cpuprobe ok=(\d+)", int),
    }

    def summary(self, row: dict) -> str:
        s = (
            f"Q{row.get('query')}: {row.get('query_ms')} ms, "
            f"{row.get('rows')} rows, match={row.get('match')}"
        )
        if row.get("net_ms") is not None:
            s += f", net {row['net_ms']} ms over {row.get('net_calls')} calls"
        if row.get("mb_per_s") is not None:
            s += f", {row['mb_per_s']} MB/s/conn"
        if row.get("poll_gap_us_avg") is not None:
            s += (
                f", poll gap {row['poll_gap_us_avg']} us avg / "
                f"{row.get('poll_gap_us_max')} us max"
            )
        return s

    # Checked once per scale factor, not once per run: a sweep over queries at
    # one sf would otherwise ask S3 the same question for every point.
    _checked: set = set()

    def require_data(self, bucket: str, sf: str) -> None:
        """Fail before anything is launched if the bucket has no data at this
        scale factor. `just setup` defaults to sf=1, so asking for --sf 10
        against a freshly provisioned bucket otherwise gets as far as booting
        an instance before the guest reports a 404 it cannot explain."""
        if sf in self._checked:
            return
        key = f"tpch/sf{sf}/lineitem.parquet"
        try:
            boto3.client("s3", region_name=os.environ["AWS_REGION"]).head_object(
                Bucket=bucket, Key=key
            )
        except Exception:
            raise SystemExit(
                f"no TPC-H data at sf={sf} in s3://{bucket} (looked for {key}).\n"
                f"Generate and upload it with:\n"
                f"    just setup apps/bench/duckdb-tpch {sf}\n"
                f"That recipe takes the scale factors as its argument and "
                f"defaults to 1, so a bucket set up without one has only sf=1."
            )
        self._checked.add(sf)

    def build(self, cfg: dict, ip: str) -> None:
        """Rebuild only when workers/conns changed (main.o depends on a stamp
        file miniosv.mk only touches when MININET_* actually moved); query/sf
        always get a fresh boot-args sector, which costs nothing to redo."""
        bucket = os.environ["AWS_BUCKET"]
        region = os.environ["AWS_REGION"]
        host = f"{bucket}.s3.{region}.amazonaws.com"
        self.require_data(bucket, cfg["sf"])
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

        # threads=0 means "DuckDB's own default"; don't pass the flag at all.
        thr = f" --threads {cfg['threads']}" if cfg.get("threads") else ""
        mem = f" --memlimit {cfg['memlimit']}" if cfg.get("memlimit") else ""
        hlog = " --httplog" if str(cfg.get("httplog") or "") == "1" else ""
        # A different executable, not a flag on tpch: the ladder shares only
        # the thread count with it, and asking `tpch` to ignore --sf, the
        # query list and the bucket would make its argument parse a lie.
        if str(cfg.get("cpuprobe") or "") == "1":
            # No thread count: the ladder's par_t1/par_tall pair is what sets
            # it, and overriding it would make par_tall mean something else.
            args = "cpuprobe"
        else:
            args = f"tpch --sf {cfg['sf']}{thr}{mem}{hlog} {cfg['query']}"
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
            crashed = False
            if iid:  # billing starts here: cap it
                for _ in range(self.max_vm_seconds):
                    text = log.read_text(errors="replace")
                    if re.search(r"^(COMPLETE|INCOMPLETE):", text, re.M):
                        break
                    # A guest that has died says so and then says nothing ever
                    # again, so waiting for its verdict means paying out the
                    # whole vm cap for a machine that is already gone. Stop on
                    # the death rattle instead.
                    if m := CRASH.search(text):
                        crashed = True
                        print(
                            f"    guest died: {m.group(0).strip()} — "
                            f"terminating {iid} now",
                            flush=True,
                        )
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
        # Recorded, not just acted on: a crash and a query that merely returned
        # nothing both come out as valid=False, and only one of them means the
        # image is broken.
        row["crashed"] = crashed or bool(CRASH.search(text))
        row["log"] = log.name
        return row

    def valid(self, row: dict) -> bool:
        """No conns/syn_retries here (that is the network stack's own gate,
        exercised by smoltcp-s3); this bench's gate is the guest's own verdict
        plus, when the scale factor had a canned answer, that it matched.

        A cpuprobe row has no query and no answer to match; what it must have
        is every step of the ladder, because a row missing one is a row whose
        columns cannot be subtracted from the other arm's."""
        if not bool(row.get("complete")):
            return False
        # A run that completed but whose timing never reached us is not a data
        # point. The serial console is not line-atomic, so a kernel message can
        # land on top of the `Qnn:` line and take the run's only number with
        # it; without this the row counts toward "n valid" while contributing
        # an empty cell, and a plot draws a box over fewer reps than its
        # caption claims. Same gate as the duckdb-linux driver's.
        if str(row.get("cpuprobe") or "") != "1" and row.get("query_ms") is None:
            return False
        if str(row.get("cpuprobe") or "") == "1":
            return row.get("probe_failed") is None and row.get("cpuprobe_ok") == len(
                PROBE_STEP_NAMES
            )
        return row.get("match") != "no"


if __name__ == "__main__":
    runner.cli(DuckdbTpch())
