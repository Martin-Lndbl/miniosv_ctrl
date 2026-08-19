#!/usr/bin/env python3
"""Sweep the Tier-1 Linux baseline; same CSV shape and validity gate as the
smoltcp sweep, so the two AGGREGATE numbers are comparable (caveats in
docs/tier1-linux-baseline.md §1).

    just bench competitors/linux-s3 --sweep mode=stock,parity --reps 3 --interleave
    just bench competitors/linux-s3 --sweep conns=1,2,4,8,16,24 --mode stock

Unlike the unikernel: knobs are runtime, so an axis value needs no rebuild; and
there is no `just deploy` — a stock AL2023 AMI boots scripts/instance.py as
user-data and powers itself off. Sweep machinery is ../runner.py.
"""

from __future__ import annotations

import os
import re
import gzip
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3  # noqa: E402
import runner  # noqa: E402
from runner import ROOT, Bench, ec2, parse, size  # noqa: E402

BENCH = "competitors/linux-s3"
SCRIPTS = ROOT / BENCH / "scripts"
# Overridable with --ami; the hook for a NixOS image (docs §9.10).
SSM_AMI = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64"
# cloud-init's hard limit on user-data.
USER_DATA_MAX = 16 * 1024


class LinuxS3(Bench):
    name = "linux-s3"
    os_name = "linux"
    knobs = {
        "workers": ("BENCH_WORKERS", size),
        "conns": ("BENCH_CONNS_PER_WORKER", size),
        "block": ("BENCH_BLOCK_SIZE", size),
        # stock  = as-shipped AL2023, all 32 cores
        # capped = the unikernel's 8-core budget, every Linux feature kept
        # parity = capped, plus jumbo/GRO/delayed-ACK/autotuning removed
        "mode": ("MODE", str),
    }
    # The point the existing smoltcp rows sit at.
    defaults = {"workers": 8, "conns": 24, "block": 128 << 20, "mode": "stock"}
    instance_tag = "linux-s3-bench"
    default_instance = "c6in.8xlarge"
    max_vm_seconds = 900  # mostly waiting for EC2 to expose the console

    metrics = {
        # No `rss:` line: this stack does not steer by RSS, and nothing clamps
        # the worker count, so requested == actual.
        "workers_actual": (r"^bench: (\d+) workers", int),
        # Read from the guest: a stubbed run discards ciphertext instead of
        # decrypting it, and is not the same experiment.
        "tls_stub": (r"^bench:.*tls_stub=(\w+)", lambda v: v == "true"),
        # An http row measured no TLS, so it is not comparable to an https one.
        "scheme": (r"^bench:.*scheme=(\w+)", str),
        "gbps": (r"AGGREGATE:.*?, ([\d.]+) Gbps", float),
        "mb_per_s": (r"AGGREGATE:.*?=> ([\d.]+) MB/s", float),
        "elapsed_s": (r"AGGREGATE: [\d.]+ MiB in ([\d.]+) s", float),
        "conns_clean": (r"^connections\s+: (\d+)/", int),
        "conns_total": (r"^connections\s+: \d+/(\d+)", int),
        "syn_retries": (r"^syn retries\s+: (\d+)", int),
        "misrouted": (r"^misrouted rx\s+: (\d+)", int),
        "setup_ms": (r"^setup\s+: (\d+) ms", int),
        # setup_ms sums overlapping waits, so it is a marker, not a duration
        # that can be subtracted from elapsed_s. These are the wall-clock pair.
        "gbps_transfer": (r"TRANSFER:.*?, ([\d.]+) Gbps", float),
        "setup_wall_s": (r"TRANSFER:.*?\(setup ([\d.]+) s excluded\)", float),
        "bytes": (r"\((\d+) bytes\)", int),
        # Read back from the guest, not from what we asked for. Port is no
        # longer fixed at 443: http dials 80.
        "target_ip": (r"^target: ([\d.]+):\d+", str),
        "target_port": (r"^target: [\d.]+:(\d+)", int),
    }

    def __init__(self) -> None:
        self._built = False
        self.ami: str | None = None

    def add_arguments(self, ap) -> None:
        ap.add_argument(
            "--ami", default=None, help="override the AL2023 AMI (the NixOS-image hook)"
        )

    # No `valid()` override: EC2 shaping is recorded in `allowance_exceeded`,
    # not disqualifying, and the unikernel side has no counterpart to gate on.

    # -- build ---------------------------------------------------------------

    def build(self, cfg: dict, ip: str) -> None:
        """Static build, upload, bucket-policy check. Independent of `cfg`:
        knobs are runtime, so one build serves the whole sweep."""
        if self._built:
            print("    binary already built this sweep (knobs are runtime here)")
            return
        r = subprocess.run(
            ["just", "setup", BENCH], cwd=ROOT, capture_output=True, text=True
        )
        if r.returncode:
            raise SystemExit(f"setup/build failed:\n{r.stdout}\n{r.stderr}")
        print("    " + "\n    ".join(r.stdout.strip().splitlines()[-6:]))
        self._built = True

    # -- launch --------------------------------------------------------------

    def resolve_ami(self, override: str | None) -> str:
        if override:
            return override
        ssm = boto3.client("ssm", region_name=os.environ["AWS_REGION"])
        return ssm.get_parameter(Name=SSM_AMI)["Parameter"]["Value"]

    def user_data(self, cfg: dict, ip: str, run_id: str) -> str:
        """A `CONFIG` literal, then instance.py verbatim. cloud-init runs
        user-data through its shebang, so it is just a Python program."""
        conf = {
            "AWS_BUCKET": os.environ["AWS_BUCKET"],
            "AWS_REGION": os.environ["AWS_REGION"],
            "AWS_BUCKET_SIZE": os.environ.get("AWS_BUCKET_SIZE", "10G"),
            "AWS_TARGET_IP": ip,
            "BENCH_WORKERS": str(cfg["workers"]),
            "BENCH_CONNS_PER_WORKER": str(cfg["conns"]),
            "BENCH_BLOCK_SIZE": str(cfg["block"]),
            "BENCH_TLS_STUB": os.environ.get("BENCH_TLS_STUB", "0"),
            # "http" drops TLS and dials 80.
            "BENCH_SCHEME": os.environ.get("BENCH_SCHEME", "https"),
            "MODE": str(cfg["mode"]),
            "RUN_ID": run_id,
        }
        body = (SCRIPTS / "instance.py").read_text()
        # Drop instance.py's own shebang; the one at the top wins.
        body = body.split("\n", 1)[1] if body.startswith("#!") else body
        return "#!/usr/bin/env python3\nCONFIG = {}\n{}".format(json.dumps(conf), body)

    def user_data_blob(self, cfg: dict, ip: str, run_id: str) -> bytes:
        """gzipped: instance.py is ~17 KB and EC2 caps user-data at 16.
        cloud-init sniffs the gzip magic before looking at the shebang."""
        raw = self.user_data(cfg, ip, run_id).encode()
        blob = gzip.compress(raw)
        if len(blob) > USER_DATA_MAX:
            raise SystemExit(
                f"user-data is {len(blob)} bytes gzipped (from {len(raw)}), over "
                f"cloud-init's {USER_DATA_MAX}; fetch instance.py from S3 instead"
            )
        print(f"    user-data: {len(raw)} bytes -> {len(blob)} gzipped")
        return blob

    def run_once(self, instance: str, logdir: Path, cfg: dict, ip: str) -> dict:
        logdir.mkdir(parents=True, exist_ok=True)
        run_id = f"{instance}-{cfg['mode']}-{int(time.time())}"
        log = logdir / f"run-{run_id}.log"
        # Same tag, different owner: report, never touch.
        if up := self.live():
            print(f"    note: {len(up)} other {self.instance_tag} instance(s) "
                  f"up, not ours: {', '.join(up)}", flush=True)

        c = ec2()
        r = c.run_instances(
            ImageId=self.ami,
            InstanceType=instance,
            MinCount=1,
            MaxCount=1,
            SubnetId=os.environ["AWS_SUBNET"],
            UserData=self.user_data_blob(cfg, ip, run_id),
            # instance.py's poweroff then terminates rather than stopping.
            InstanceInitiatedShutdownBehavior="terminate",
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": self.instance_tag},
                        {"Key": "bench", "Value": "linux-s3"},
                    ],
                }
            ],
        )
        iid = r["Instances"][0]["InstanceId"]
        print(f"  Instance running: {iid}", flush=True)

        # Billing starts here: everything below must reach the terminate call.
        text = ""
        try:
            deadline = time.time() + self.max_vm_seconds
            while time.time() < deadline:
                time.sleep(15)
                try:
                    out = c.get_console_output(InstanceId=iid, Latest=True)
                    got = out.get("Output", "") or ""
                except Exception as e:  # not yet available
                    got = ""
                    print(f"    console: {e}", flush=True)
                # The buffer truncates from the front: keep the longest read.
                if len(got) > len(text):
                    text = got
                    log.write_text(text)
                if re.search(r"^(COMPLETE|INCOMPLETE):", text, re.M):
                    break
        finally:
            c.terminate_instances(InstanceIds=[iid])  # only what we launched

        log.write_text(text)

        row = parse(text, self.metrics)
        row["complete"] = bool(re.search(r"^COMPLETE:", text, re.M))
        row["instance_id"] = iid
        row["log"] = log.name
        # Both spellings: stored logs still say RUN INVALID.
        row["allowance_exceeded"] = len(
            re.findall(r"allowance_exceeded.*(?:RUN INVALID|EC2 SHAPED)", text)
        )
        return row


def main() -> None:
    bench = LinuxS3()
    # Resolved before runner parses argv; a dry run must not pay an SSM call.
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
