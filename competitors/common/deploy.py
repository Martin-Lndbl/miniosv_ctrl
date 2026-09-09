#!/usr/bin/env python3
"""Launch a Linux baseline on EC2 and bring back its console.

    just --justfile competitors/<bench>/justfile deploy c7i.large

Same shape as scripts/bench/linux-s3/bench.py's launch path, minus the sweep.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[2]  # competitors/common/ -> repo root
SCRIPTS = Path(__file__).resolve().parent
SSM_AMI = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-{arch}"
USER_DATA_MAX = 16 * 1024


def user_data(key: str, no_throttle: bool = False) -> bytes:
    conf = {
        "AWS_BUCKET": os.environ["AWS_BUCKET"],
        "AWS_REGION": os.environ["AWS_REGION"],
        "BENCH_KEY": key,
        "PERF_NO_THROTTLE": "1" if no_throttle else "0",
    }
    body = (SCRIPTS / "instance.py").read_text()
    body = body.split("\n", 1)[1] if body.startswith("#!") else body
    raw = "#!/usr/bin/env python3\nCONFIG = {}\n{}".format(
        json.dumps(conf), body
    ).encode()
    # gzipped: cloud-init sniffs the magic before the shebang; EC2 caps 16 KiB.
    blob = gzip.compress(raw)
    if len(blob) > USER_DATA_MAX:
        raise SystemExit(
            f"user-data is {len(blob)} bytes gzipped (from {len(raw)}), over "
            f"cloud-init's {USER_DATA_MAX}"
        )
    print(f"user-data: {len(raw)} bytes -> {len(blob)} gzipped")
    return blob


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("instance", nargs="?", default="c7i.large")
    ap.add_argument("--key", default="linux-sample",
                    help="S3 object under bin/, also the results prefix")
    ap.add_argument("--results", default="pmc-sample",
                    help="results/<dir> the log and CSV land in")
    ap.add_argument("--marker", default="pmc-sample",
                    help="line prefix the guest prints; '<marker>: done' ends the run")
    ap.add_argument("--csv-tag", default="SAMPLE",
                    help="CSV rows are the lines tagged with this")
    # A separate series, not a correction: stock is what a user gets, and the
    # throttle is a deliberate kernel safety valve rather than a defect.
    ap.add_argument("--no-throttle", action="store_true",
                    help="disable Linux's perf rate throttling in the guest")
    ap.add_argument("--ami", default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument(
        "--deadline",
        type=int,
        default=900,
        help="ceiling on the instance's life; billing runs until terminate",
    )
    a = ap.parse_args()

    region = os.environ["AWS_REGION"]
    ec2 = boto3.client("ec2", region_name=region)

    arch = "arm64" if re.match(r"^[a-z]+\dg", a.instance) else "x86_64"
    ami = a.ami or boto3.client("ssm", region_name=region).get_parameter(
        Name=SSM_AMI.format(arch=arch)
    )["Parameter"]["Value"]
    print(f"instance : {a.instance} ({arch})\nami      : {ami}")

    # The variant is in the filename because run_id() in the plot keys the
    # series off it -- two Linux configurations sharing a name would pool into
    # one, which is exactly the mistake that has to be avoided here.
    tag = "linuxnt" if a.no_throttle else "linux"
    out = a.out or (
        ROOT / "results" / a.results
        / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{tag}-{a.instance}.log"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    r = ec2.run_instances(
        ImageId=ami,
        InstanceType=a.instance,
        MinCount=1,
        MaxCount=1,
        SubnetId=os.environ["AWS_SUBNET"],
        UserData=user_data(a.key, a.no_throttle),
        # Terminate rather than stop, so no EBS volume is left billing.
        InstanceInitiatedShutdownBehavior="terminate",
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [{"Key": "Name", "Value": a.key},
                         {"Key": "bench", "Value": a.key}],
            }
        ],
    )
    iid = r["Instances"][0]["InstanceId"]
    print(f"launched : {iid}", flush=True)

    # Billing starts here: every path below must reach the terminate call.
    text = ""
    try:
        deadline = time.time() + a.deadline
        while time.time() < deadline:
            time.sleep(15)
            try:
                got = ec2.get_console_output(
                    InstanceId=iid, Latest=True
                ).get("Output", "") or ""
            except Exception as e:
                print(f"  console: {e}", flush=True)
                continue
            # The buffer truncates from the front: keep the longest read.
            if len(got) > len(text):
                text = got
                out.write_text(text)
                print(f"  console: {len(text)} bytes", flush=True)
            if f"{a.marker}: done" in text:  # prefixed by cloud-init too
                break
    finally:
        ec2.terminate_instances(InstanceIds=[iid])
        print(f"terminated {iid}", flush=True)

    out.write_text(text)

    # Searched, not anchored: cloud-init stamps every console line with
    # "[    6.71] cloud-init[2345]: ", so a row never starts the line.
    # Comma-separated, one CSV per tag: a benchmark may emit more than one
    # block per run with different schemas, and merging them into one file
    # would produce a CSV with two headers. Every benchmark here currently
    # emits a single tag; the split exists for the ones that will not.
    tags = [t for t in (t.strip() for t in a.csv_tag.split(",")) if t]
    missing = []
    for tag in tags:
        rows = []
        for ln in text.splitlines():
            m = re.search(rf"#?{tag},(.*)$", ln.rstrip("\r"))
            if m:
                rows.append(m.group(1))
        if rows:
            csv = out.with_name(out.stem + f"-{tag.lower()}.csv")
            csv.write_text("\n".join(rows) + "\n")
            print(f"wrote {csv} ({len(rows) - 1} rows)")
        else:
            missing.append(tag)
    print(f"wrote {out}")
    if missing:
        print(f"no rows for {', '.join(missing)}; read the log", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
