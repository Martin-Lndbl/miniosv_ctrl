#!/usr/bin/env python3
"""Sweep one compile-time knob of apps/bench/smoltcp-s3 and record a CSV row per run.

    just bench smoltcp-s3 --sweep conns=1,2,3,4,6,8,12,16,24
    just bench smoltcp-s3 --sweep workers=1,2,4,8 --conns 4
    just bench smoltcp-s3 --sweep block=16M,64M,256M --conns 4 --reps 1
    just bench smoltcp-s3 --sweep conns=1,4 --dry-run

Any knob can be the axis; the others stay fixed and are recorded on every row,
so CSVs from different sweeps concatenate and stay interpretable. Holding block
size fixed while varying parallelism matters: the bench used to split a fixed
total across workers, so request size shrank as parallelism rose and a
"throughput vs workers" curve was also a "throughput vs request size" curve.

Knobs are compiled in, so each axis value needs its own image build;
repetitions of a value reuse it.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
BENCH = "apps/bench/smoltcp-s3"
TAG = "miniosv-loader-*"        # created only by miniosv/scripts/aws-deploy.py
MAX_VM_SECONDS = 110            # keep every run well under two minutes

KNOBS = {"workers": "BENCH_WORKERS",
         "conns": "BENCH_CONNS_PER_WORKER",
         "block": "BENCH_BLOCK_SIZE"}

# field -> (pattern, cast). The guest prints these; see osv_app_main().
METRICS = {
    "workers_actual": (r"^rss: (\d+) queues", int),
    "gbps": (r"AGGREGATE:.*?, ([\d.]+) Gbps", float),
    "mb_per_s": (r"AGGREGATE:.*?=> ([\d.]+) MB/s", float),
    "elapsed_s": (r"AGGREGATE: [\d.]+ MiB in ([\d.]+) s", float),
    "conns_clean": (r"^connections\s+: (\d+)/", int),
    "conns_total": (r"^connections\s+: \d+/(\d+)", int),
    "syn_retries": (r"^syn retries\s+: (\d+)", int),
    "misrouted": (r"^misrouted rx\s+: (\d+)", int),
    "setup_ms": (r"^setup\s+: (\d+) ms", int),
    "bytes": (r"\((\d+) bytes\)", int),
    "instance_id": (r"Instance running: (i-[0-9a-f]+)", str),
}


def size(text: str) -> int:
    """Accept 128M / 2G / 1048576, matching what .env holds."""
    m = re.fullmatch(r"(\d+)\s*([KMGT]?)i?B?", str(text).strip(), re.I)
    if not m:
        raise argparse.ArgumentTypeError(f"bad size: {text}")
    return int(m.group(1)) * {"": 1, "K": 1 << 10, "M": 1 << 20,
                              "G": 1 << 30, "T": 1 << 40}[m.group(2).upper()]


def ec2():
    return boto3.client("ec2", region_name=os.environ["AWS_REGION"])


def live() -> list[str]:
    r = ec2().describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [TAG]},
        {"Name": "instance-state-name", "Values": ["pending", "running"]}])
    return [i["InstanceId"] for x in r["Reservations"] for i in x["Instances"]]


def target_ip() -> str:
    """The guest has no resolver, so the address is compiled in. S3 rotates it
    often enough to matter (three addresses in one afternoon) and a stale value
    now fails the build-time assert, so resolve fresh rather than trust .env."""
    host = f"{os.environ['AWS_BUCKET']}.s3.{os.environ['AWS_REGION']}.amazonaws.com"
    out = subprocess.run(["getent", "ahostsv4", host],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "STREAM" in line:
            return line.split()[0]
    raise SystemExit(f"could not resolve {host}")


def build(cfg: dict, ip: str) -> None:
    """Bake this point's constants in.

    build.rs declares each knob via rerun-if-env-changed, so cargo rebuilds when
    one moves; without it a sweep would redeploy the first build's constants at
    every point while appearing to work. We must already be inside the devshell
    (the justfile recipe arranges it): its shellHook does `set -a; . .env` and
    would otherwise overwrite these on the way in.
    """
    env = {**os.environ, "AWS_TARGET_IP": ip,
           **{KNOBS[k]: str(v) for k, v in cfg.items()}}
    r = subprocess.run(["just", "build", BENCH, "-j16"],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"build failed for {cfg}:\n{r.stdout}\n{r.stderr}")


def run_once(instance: str, logdir: Path) -> dict:
    """Deploy, wait for the guest's verdict, terminate, parse.

    Termination is an API call rather than a signal: `setsid` makes the child's
    pgid resolve in ways that can kill the driver instead of the deploy, which
    has orphaned billing instances before.
    """
    logdir.mkdir(parents=True, exist_ok=True)
    log = logdir / f"deploy-{instance}-{int(time.time())}.log"
    if up := live():
        raise SystemExit(f"refusing to launch, instance still up: {up}")

    with log.open("w") as fh:
        p = subprocess.Popen(["just", "deploy", instance], cwd=ROOT,
                             stdout=fh, stderr=subprocess.STDOUT,
                             start_new_session=True)
        iid = None
        for _ in range(1500):       # wait for the instance to exist
            if m := re.search(r"Instance running: (i-[0-9a-f]+)",
                              log.read_text(errors="replace")):
                iid = m.group(1)
                break
            if p.poll() is not None:
                break
            time.sleep(1)
        if iid:                     # billing starts here: cap it
            for _ in range(MAX_VM_SECONDS):
                if re.search(r"^(COMPLETE|INCOMPLETE):",
                             log.read_text(errors="replace"), re.M):
                    break
                if p.poll() is not None:
                    break
                time.sleep(1)
            ec2().terminate_instances(InstanceIds=[iid])
        p.send_signal(2)            # let it deregister the AMI; don't wait
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass

    if stray := live():             # a launch interrupted before it logged an id
        ec2().terminate_instances(InstanceIds=stray)

    text = log.read_text(errors="replace")
    row = {k: (c(m.group(1)) if (m := re.search(pat, text, re.M)) else None)
           for k, (pat, c) in METRICS.items()}
    row["complete"] = bool(re.search(r"^COMPLETE:", text, re.M))
    row["log"] = log.name
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", default="conns=1,2,3,4,6,8,12,16,24",
                    metavar="KNOB=V1,V2", help=f"axis to vary: {'/'.join(KNOBS)}")
    ap.add_argument("--workers", type=size, default=8)
    ap.add_argument("--conns", type=size, default=24)
    ap.add_argument("--block", type=size, default=128 << 20,
                    help="bytes per request; one request per connection")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--instance", default="c6in.8xlarge",
                    help="50 Gbps sustained; c7i.8xlarge caps at 12.5 and the "
                         "bench reaches 11.85 there, so its curve is clipped by "
                         "EC2's allowance rather than by the stack")
    ap.add_argument("--out", type=Path, default=ROOT / "results/smoltcp-s3/sweep.csv")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    axis, _, raw = a.sweep.partition("=")
    if axis not in KNOBS or not raw:
        raise SystemExit(f"--sweep must be one of {list(KNOBS)}, e.g. conns=1,2,4")
    values = [size(v) for v in raw.split(",")]
    base = {"workers": a.workers, "conns": a.conns, "block": a.block}
    for req in ("AWS_BUCKET", "AWS_REGION"):
        if req not in os.environ:
            raise SystemExit(f"{req} missing — run `just setup {BENCH}`")

    plan = [(v, r) for v in values for r in range(1, a.reps + 1)]
    gib = sum({**base, axis: v}["workers"] * {**base, axis: v}["conns"]
              * {**base, axis: v}["block"] for v, _ in plan) / (1 << 30)
    print(f"instance : {a.instance}\naxis     : {axis} = {values}"
          f"\nfixed    : {({k: v for k, v in base.items() if k != axis})}"
          f"\nruns     : {len(plan)} ({len(values)} builds x {a.reps} reps)"
          f"\ntransfer : {gib:.1f} GiB total\nout      : {a.out}")
    if a.dry_run:
        return 0

    ip = target_ip()
    print(f"target   : {ip}")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(a.out) if a.out.exists() else pd.DataFrame()
    if not df.empty:
        print(f"resuming : {len(df)} rows present")

    built = None
    for value, rep in plan:
        cfg = {**base, axis: value}
        if not df.empty and ((df.get("axis") == axis)
                             & (df.get("axis_value") == value)
                             & (df.get("rep") == rep)).any():
            continue
        if cfg != built:
            print(f"\n=== building {cfg} ===", flush=True)
            build(cfg, ip)
            built = cfg

        print(f"--- {axis}={value} rep={rep} ---", flush=True)
        row = run_once(a.instance, a.out.parent / "logs")
        row |= {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "instance": a.instance, "axis": axis, "axis_value": value,
                "rep": rep, **cfg}
        # A point counts only if everything transferred and the RSS model held.
        row["valid"] = bool(row["complete"] and row["syn_retries"] == 0
                            and row["misrouted"] == 0
                            and row["conns_total"] == row["conns_clean"])
        if row["bytes"] and row["elapsed_s"]:
            row["est_rx_pps"] = round(row["bytes"] / 1460 / row["elapsed_s"])
        if row["gbps"] and row["workers_actual"]:
            row["gbps_per_worker"] = round(row["gbps"] / row["workers_actual"], 4)

        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        df.to_csv(a.out, index=False)   # a partial sweep survives interruption
        print(f"  {row['gbps']} Gbps, {row['workers_actual']} workers, "
              f"{row['conns_clean']}/{row['conns_total']} clean, valid={row['valid']}")

    print(f"\nwrote {a.out} ({len(df)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
