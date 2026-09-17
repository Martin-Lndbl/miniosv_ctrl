#!/usr/bin/env python3
"""Sweep TPC-H queries against real DuckDB-on-Linux, reading the same S3
parquet apps/bench/duckdb-tpch does -- the baseline that driver's numbers get
compared against.

    just bench competitors/duckdb-linux --sweep query=1,3,6,10,12
    just bench competitors/duckdb-linux --sweep query=6 --sf 0.1

Unlike duckdb-tpch: no image to build, no boot args to write. A stock AL2023
AMI boots scripts/instance.py as user-data, which fetches the real duckdb CLI
+ httpfs extension from S3 (just setup competitors/duckdb-linux) and runs.
Both knobs are runtime, so `build()` only has to make sure the binary and
extension are actually in the bucket -- once per sweep, like linux-s3's.

Prints the same log-line shapes as apps/miniduckdb/miniosv/main.cc's tpch
executable, so this driver's `metrics` are the ones from
scripts/bench/duckdb-tpch/bench.py verbatim -- the CSV columns line up for a
direct comparison. Sweep machinery is ../runner.py.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3  # noqa: E402
import runner  # noqa: E402
from runner import ROOT, Bench, ec2, parse  # noqa: E402

# cloud-init's scripts-user module re-emits the script's stdout dmesg-style --
# "[   12.8] cloud-init[2342]: COMPLETE: ..." -- so every ^-anchored regex
# below would otherwise never match (confirmed: a real run printed COMPLETE
# and match=yes, but every field came back None because nothing matched).
# Not anchored, and replaced with a newline rather than dropped: the getty
# login prompt has no trailing newline of its own, so the first prefixed line
# of real output arrives glued onto "ip-1-2-3-4 login: " with nothing to
# split them -- an unanchored strip alone still leaves that line short of a
# real line start for every ^-anchored pattern after it.
CLOUD_INIT_PREFIX = re.compile(r"\[[\s\d.]+\] cloud-init\[\d+\]: ")


def strip_cloud_init(text: str) -> str:
    return CLOUD_INIT_PREFIX.sub("\n", text)

BENCH = "competitors/duckdb-linux"
SCRIPTS = ROOT / BENCH / "scripts"
SSM_AMI = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64"
USER_DATA_MAX = 16 * 1024


class DuckdbLinux(Bench):
    name = "duckdb-linux"
    os_name = "linux"
    knobs = {
        "query": (None, int),
        "sf": (None, str),
        # An S3 address to pin the bucket name to, or "" to resolve normally.
        # miniOSv compiles in one address; this makes Linux ask the same
        # question instead of spreading over whatever DNS returns.
        "pin": (None, str),
        # Receive-path parity with mininet. "off" disables GRO, so the stack
        # sees each frame rather than one coalesced segment; queues sets the
        # NIC's combined RX/TX count, which is mininet's worker count. Both
        # empty leave the NIC as the kernel set it up -- the stock arm.
        "gro": (None, str),
        "queues": (None, str),
        # "1" adds a second pass per query with DuckDB's HTTP request log on,
        # reported as an `HTTP STATS:` line. Diagnostic: it measures requests
        # inside DuckDB, above whichever client is underneath, which is where
        # the miniOSv arm can be compared to this one at the same layer.
        "httplog": (None, str),
        # "1" swaps the S3 run for the no-network CPU ladder main.cc's
        # `cpuprobe` executable runs. Same steps, same sizes, same order, so
        # the two arms' PROBE rows subtract directly.
        "cpuprobe": (None, str),
        # DuckDB's thread count. Empty is its own default of one per cpu.
        # Here because apps/bench/duckdb-tpch has had it all along, and a
        # tuning knob on one arm only is not a comparison.
        "threads": (None, int),
    }
    defaults = {"query": 6, "sf": "1", "pin": "", "gro": "", "queues": "",
                "httplog": "", "cpuprobe": "", "threads": 0}
    instance_tag = "duckdb-linux-bench"
    default_instance = "c7i.large"  # matches apps/bench/duckdb-tpch's default
    max_vm_seconds = 300
    headline_metric = "query_ms"
    headline_agg = "min"
    headline_unit = "ms"

    metrics = {"pinned_ip": (r"^pinned \S+ -> ([\d.]+)", str),
               # Read back off the NIC, not echoed from the request, so these
               # say what the run actually had rather than what it asked for.
               "nic_iface": (r"^nic: iface=(\S+)", str),
               "nic_gro": (r"^nic: iface=\S+ gro=(\S+)", str),
               "nic_queues": (r"^nic: iface=\S+ gro=\S+ combined=(\d+)", int),
               "nic_tune_failed": (r"^nic: (FAILED)", str)} | {
        "query_ms": (r"^Q\d+: ([\d.]+) ms,", float),
        "rows": (r"^Q\d+: [\d.]+ ms, (\d+) rows", int),
        "match": (r"^Q\d+: [\d.]+ ms, \d+ rows, match=(\w+)", str),
        "queries_ok": (r"^TPCH SUMMARY: ok=(\d+)", int),
        "queries_total": (r"^TPCH SUMMARY: ok=\d+ total=(\d+)", int),
        # Same clock as the per-query line, summed. Only a fallback for when
        # that line does not survive the console; see run_once.
        "summary_ms": (r"^TPCH SUMMARY: ok=\d+ total=\d+ ms=([\d.]+)", float),
        # From the httplog pass. `http_concurrency` is the mean number of
        # requests in flight, which is what decides a latency-bound query and
        # is directly comparable to the miniOSv arm's net_ms/query_ms.
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
        "http_profile_ms": (r"^HTTP STATS: .*\bprofile_ms=([\d.]+)", float),
        "http_failed": (r"^HTTP STATS: (FAILED)", str),
        # From the cpuprobe ladder. Named per step so one CSV row holds the
        # whole ladder and the two arms' rows subtract column by column.
        "probe_range_scan_ms": (r"^PROBE: name=range_scan ms=([\d.]+)", float),
        "probe_hash_agg_ms": (r"^PROBE: name=hash_agg ms=([\d.]+)", float),
        "probe_dbgen_ms": (r"^PROBE: name=dbgen ms=([\d.]+)", float),
        "probe_par_t1_ms": (r"^PROBE: name=par_t1 ms=([\d.]+)", float),
        "probe_par_tall_ms": (r"^PROBE: name=par_tall ms=([\d.]+)", float),
        "probe_par_t1_4x_ms": (r"^PROBE: name=par_t1_4x ms=([\d.]+)", float),
        "probe_par_tall_4x_ms": (r"^PROBE: name=par_tall_4x ms=([\d.]+)", float),
        "probe_q01_local_ms": (r"^PROBE: name=q01_local ms=([\d.]+)", float),
        "probe_q06_local_ms": (r"^PROBE: name=q06_local ms=([\d.]+)", float),
        "probe_failed": (r"^PROBE: name=\S+ (FAILED)", str),
        "cpuprobe_ok": (r"^(?:IN)?COMPLETE: cpuprobe ok=(\d+)", int),
        "checked": (r"checked=(\d+)", int),
        "matched": (r"matched=(\d+)", int),
    }

    def __init__(self) -> None:
        self._built = False
        self.ami: str | None = None

    def summary(self, row: dict) -> str:
        return (
            f"Q{row.get('query')}: {row.get('query_ms')} ms, "
            f"{row.get('rows')} rows, match={row.get('match')}"
        )

    # -- build -----------------------------------------------------------

    def build(self, cfg: dict, ip: str) -> None:
        """Fetch+upload duckdb/httpfs once per sweep; both knobs are runtime
        (boot args to instance.py), so nothing else needs redoing per point."""
        if self._built:
            print("    duckdb/httpfs already uploaded this sweep")
            return
        r = subprocess.run(
            ["just", "setup", BENCH], cwd=ROOT, capture_output=True, text=True
        )
        if r.returncode:
            raise SystemExit(f"setup failed:\n{r.stdout}\n{r.stderr}")
        print("    " + "\n    ".join(r.stdout.strip().splitlines()[-6:]))
        self._built = True

    # -- launch ------------------------------------------------------------

    def resolve_ami(self, override: str | None) -> str:
        if override:
            return override
        ssm = boto3.client("ssm", region_name=os.environ["AWS_REGION"])
        return ssm.get_parameter(Name=SSM_AMI)["Parameter"]["Value"]

    def user_data(self, cfg: dict) -> str:
        conf = {
            "AWS_BUCKET": os.environ["AWS_BUCKET"],
            "AWS_REGION": os.environ["AWS_REGION"],
            "BENCH_SF": str(cfg["sf"]),
            "BENCH_QUERIES": str(cfg["query"]),
            "BENCH_PIN_IP": str(cfg.get("pin") or ""),
            "BENCH_GRO": str(cfg.get("gro") or ""),
            "BENCH_NIC_QUEUES": str(cfg.get("queues") or ""),
            "BENCH_HTTP_LOG": str(cfg.get("httplog") or ""),
            "BENCH_CPU_PROBE": str(cfg.get("cpuprobe") or ""),
            "BENCH_THREADS": str(cfg.get("threads") or ""),
        }
        body = (SCRIPTS / "instance.py").read_text()
        body = body.split("\n", 1)[1] if body.startswith("#!") else body
        return "#!/usr/bin/env python3\nCONFIG = {}\n{}".format(json.dumps(conf), body)

    def user_data_blob(self, cfg: dict) -> bytes:
        raw = self.user_data(cfg).encode()
        blob = gzip.compress(raw)
        if len(blob) > USER_DATA_MAX:
            raise SystemExit(
                f"user-data is {len(blob)} bytes gzipped (from {len(raw)}), over "
                f"cloud-init's {USER_DATA_MAX}"
            )
        print(f"    user-data: {len(raw)} bytes -> {len(blob)} gzipped")
        return blob

    def run_once(self, instance: str, logdir: Path, cfg: dict, ip: str) -> dict:
        logdir.mkdir(parents=True, exist_ok=True)
        run_id = f"{instance}-q{cfg['query']}-{int(time.time())}"
        log = logdir / f"run-{run_id}.log"
        if up := self.live():
            print(
                f"    note: {len(up)} other {self.instance_tag} instance(s) "
                f"up, not ours: {', '.join(up)}",
                flush=True,
            )

        c = ec2()
        r = c.run_instances(
            ImageId=self.ami,
            InstanceType=instance,
            MinCount=1,
            MaxCount=1,
            SubnetId=os.environ["AWS_SUBNET"],
            UserData=self.user_data_blob(cfg),
            InstanceInitiatedShutdownBehavior="terminate",
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": self.instance_tag},
                        {"Key": "bench", "Value": "duckdb-linux"},
                    ],
                }
            ],
        )
        iid = r["Instances"][0]["InstanceId"]
        print(f"  Instance running: {iid}", flush=True)

        text = ""
        try:
            deadline = time.time() + self.max_vm_seconds
            while time.time() < deadline:
                time.sleep(10)
                try:
                    out = c.get_console_output(InstanceId=iid, Latest=True)
                    got = out.get("Output", "") or ""
                except Exception as e:
                    got = ""
                    print(f"    console: {e}", flush=True)
                if len(got) > len(text):
                    text = strip_cloud_init(got)
                    log.write_text(text)
                if re.search(r"^(COMPLETE|INCOMPLETE):", text, re.M):
                    break
        finally:
            c.terminate_instances(InstanceIds=[iid])

        log.write_text(text)
        row = parse(text, self.metrics)
        row["complete"] = bool(re.search(r"^COMPLETE:", text, re.M))
        row["instance_id"] = iid
        row["log"] = log.name

        # The serial console is not line-atomic: a kernel message can land on
        # top of the "Qnn: ... ms" line and take the run's only number with it,
        # leaving a row that completed but measures nothing. TPCH SUMMARY's
        # ms is the same clock summed over the queries, so for a single-query
        # run it is that number exactly -- recover it rather than spend another
        # instance, and record that it was recovered.
        if row.get("query_ms") is None and row.get("queries_total") == 1 \
                and row.get("summary_ms") is not None:
            row["query_ms"] = row["summary_ms"]
            row["query_ms_from_summary"] = True
        return row

    def valid(self, row: dict) -> bool:
        if not bool(row.get("complete")):
            return False
        # A cpuprobe row has no query and no answer to match; what it must
        # have is every step of the ladder, because a row missing one is a row
        # whose columns cannot be subtracted from the other arm's.
        if str(row.get("cpuprobe") or "") == "1":
            return row.get("probe_failed") is None and row.get("cpuprobe_ok") == 9
        if row.get("match") == "no":
            return False
        # A run that completed but whose timing never reached us is not a data
        # point. Without this it counts toward "n valid" while contributing an
        # empty cell, so the plot quietly draws a box over fewer reps than the
        # caption claims.
        if row.get("query_ms") is None:
            return False
        # A parity run whose NIC tune did not take is not a parity run. Compare
        # what came back off the device against what was asked for, so a
        # missing ethtool or a queue count the driver clamped shows up as an
        # invalid row rather than as a number next to the miniOSv one.
        if row.get("nic_tune_failed"):
            return False
        if row.get("gro") and str(row["gro"]) != str(row.get("nic_gro")):
            return False
        if row.get("queues") and int(row["queues"]) != (row.get("nic_queues") or -1):
            return False
        return True


def main() -> None:
    bench = DuckdbLinux()
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
