#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pandas as pd
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
# Sweepable, but a runner parameter rather than a compiled-in knob: the image is
# identical at every point, only the machine it is deployed to changes.
INSTANCE_AXIS = "instance"
# One SYN-to-Established round trip on an unloaded path, us. The duckdb-tpch
# arm, which dials on demand and so never queues, measures 1.1-1.2 ms.
SETUP_BASELINE_US = 1200


def notify(msg: str, title: str, tags: str = "") -> None:
    """Push to BENCH_PUSH_URL if set; never fails a sweep."""
    url = os.environ.get("BENCH_PUSH_URL", "").strip()
    if not url:
        return
    import urllib.request

    with contextlib.suppress(Exception):
        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=msg.encode(),
                method="POST",
                headers={
                    "Title": title,
                    "Priority": "high",
                    "Tags": tags,
                    # Python-urllib's default UA is 403'd by some endpoints.
                    "User-Agent": "miniosv-bench/1.0",
                },
            ),
            timeout=10,
        ).read()


def size(text: str) -> int:
    """Accept 128M / 2G / 1048576, matching what .env holds."""
    m = re.fullmatch(r"(\d+)\s*([KMGT]?)i?B?", str(text).strip(), re.I)
    if not m:
        raise argparse.ArgumentTypeError(f"bad size: {text}")
    return (
        int(m.group(1))
        * {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}[
            m.group(2).upper()
        ]
    )


# Both stacks print the same summary lines on purpose, and one validity gate
# reads both, so the patterns live here instead of drifting in two drivers.
# `workers_actual` is not here: the guest reports RSS queues, the baseline
# reports threads it was asked for.
COMMON_METRICS = {
    # Read from the guest: a stubbed run discards ciphertext instead of
    # decrypting it, and an http row measured no TLS at all. Neither is
    # comparable to a plain https row.
    "tls_stub": (r"^bench:.*tls_stub=(\w+)", lambda v: v == "true"),
    "scheme": (r"^bench:.*scheme=(\w+)", str),
    "gbps": (r"AGGREGATE:.*?, ([\d.]+) Gbps", float),
    "mb_per_s": (r"AGGREGATE:.*?=> ([\d.]+) MB/s", float),
    "elapsed_s": (r"AGGREGATE: [\d.]+ MiB in ([\d.]+) s", float),
    "conns_clean": (r"^connections\s+: (\d+)/", int),
    "conns_total": (r"^connections\s+: \d+/(\d+)", int),
    "misrouted": (r"^misrouted rx\s+: (\d+)", int),
    # SYN on the wire to Established: one round trip, per connection. The old
    # `setup_ms` measured from `connect`, giving a conns^2/2 sum, not a time.
    "setup_conns": (r"^SETUP STATS\s*: conns=(\d+)", int),
    "setup_failed": (r"^SETUP STATS\s*: .*failed=(\d+)", int),
    "setup_us_avg": (r"^SETUP STATS\s*: .*us_avg=(\d+)", int),
    "setup_us_p50": (r"^SETUP STATS\s*: .*us_p50=(\d+)", int),
    "setup_us_p90": (r"^SETUP STATS\s*: .*us_p90=(\d+)", int),
    "setup_us_max": (r"^SETUP STATS\s*: .*us_max=(\d+)", int),
    # The CPU half, and the worst worker's dial loop.
    "dial_n": (r"^DIAL STATS\s*: dials=(\d+)", int),
    "dial_us_avg": (r"^DIAL STATS\s*: .*us_avg=(\d+)", int),
    "dial_us_p50": (r"^DIAL STATS\s*: .*us_p50=(\d+)", int),
    "dial_us_p90": (r"^DIAL STATS\s*: .*us_p90=(\d+)", int),
    "dial_us_max": (r"^DIAL STATS\s*: .*us_max=(\d+)", int),
    "dial_loop_ms": (r"^DIAL STATS\s*: .*loop_ms=([\d.]+)", float),
    # The wall-clock pair: what a fetch costs end to end, and what the stack
    # sustains once the connections exist.
    "gbps_transfer": (r"TRANSFER:.*?, ([\d.]+) Gbps", float),
    "setup_wall_s": (r"TRANSFER:.*?\(setup ([\d.]+) s excluded\)", float),
    "bytes": (r"\((\d+) bytes\)", int),
    # Read back from the guest, not from what we asked for. The port is not
    # fixed at 443: http dials 80.
    "target_ip": (r"^target: ([\d.]+):\d+", str),
    "target_port": (r"^target: [\d.]+:(\d+)", int),
    # The market the machine actually came from, which under
    # spot-or-on-demand is not always the one asked for.
    "market": (r"Instance running: i-[0-9a-f]+ \([^)]*?(spot|on-demand)\)", str),
}

MARKETS = ("on-demand", "spot", "spot-or-on-demand")


def launch(c, run_kwargs: dict, market: str = "on-demand") -> tuple[dict, str]:
    """run_instances, on the market asked for; returns the market used. Spot
    is a one-time request at the default max price (the on-demand rate),
    terminated if reclaimed; a run is minutes and a c6in.16xlarge costs
    about a tenth that way. "spot" fails if refused: a run that asked for
    spot and got on-demand would be billed at ten times what was expected.
    "spot-or-on-demand" prints the refusal and retries on-demand. Mirrors
    miniosv/scripts/aws-deploy.py, which cannot import this."""
    if market == "on-demand":
        return c.run_instances(**run_kwargs), "on-demand"
    kwargs = dict(
        run_kwargs,
        InstanceMarketOptions={
            "MarketType": "spot",
            "SpotOptions": {
                "SpotInstanceType": "one-time",
                "InstanceInterruptionBehavior": "terminate",
            },
        },
    )
    try:
        return c.run_instances(**kwargs), "spot"
    except ClientError as e:
        err = e.response.get("Error", {})
        if market != "spot-or-on-demand":
            raise SystemExit(
                f"spot requested but not provided: {err.get('Code')}: {err.get('Message')}"
            ) from e
        print(f"    spot not provided ({err.get('Code')}); falling back to on-demand", flush=True)
        return c.run_instances(**run_kwargs), "on-demand"


def interrupted(c, iid: str) -> bool:
    """Whether EC2 reclaimed a spot instance under a run. Asked before our own
    terminate, whose reason is Client.UserInitiatedShutdown."""
    try:
        r = c.describe_instances(InstanceIds=[iid])
        inst = r["Reservations"][0]["Instances"][0]
    except (ClientError, IndexError, KeyError):
        return False
    return inst.get("StateReason", {}).get("Code") == "Server.SpotInstanceTermination"


def ec2():
    return boto3.client("ec2", region_name=os.environ["AWS_REGION"])


def target_ip() -> str:
    """Resolve fresh: S3 rotated the address three times in one afternoon."""
    host = f"{os.environ['AWS_BUCKET']}.s3.{os.environ['AWS_REGION']}.amazonaws.com"
    out = subprocess.run(
        ["getent", "ahostsv4", host], capture_output=True, text=True
    ).stdout
    for line in out.splitlines():
        if "STREAM" in line:
            return line.split()[0]
    raise SystemExit(f"could not resolve {host}")


def parse(text: str, metrics: dict) -> dict:
    return {
        k: (c(m.group(1)) if (m := re.search(pat, text, re.M)) else None)
        for k, (pat, c) in metrics.items()
    }


class Bench:
    """Per-bench behaviour; subclasses implement `build` and `run_once`."""

    name: str = ""  # the stack, e.g. smoltcp-s3
    os_name: str = ""  # the OS results/ groups by, e.g. miniosv
    knobs: dict = {}  # knob -> (env var, value parser)
    defaults: dict = {}  # knob -> value when neither axis nor CLI
    metrics: dict = {}  # field -> (regex, cast), applied to the log
    instance_tag: str = ""  # EC2 Name tag, for stray cleanup
    default_instance: str = "c6in.8xlarge"
    max_vm_seconds: int = 110  # money guard: billing starts at launch
    market: str = "on-demand"  # one of MARKETS; see launch()
    # The final summary line's "best" figure: which column, which direction
    # counts as better, and its unit. Throughput benches want the max Gbps;
    # a latency bench like duckdb-tpch wants the min ms.
    headline_metric: str = "gbps"
    headline_agg: str = "max"  # "max" or "min"
    headline_unit: str = "Gbps"

    def add_arguments(self, ap: argparse.ArgumentParser) -> None:
        pass

    def build(self, cfg: dict, ip: str) -> None:
        raise NotImplementedError

    def run_once(self, instance: str, logdir: Path, cfg: dict, ip: str) -> dict:
        raise NotImplementedError

    def summary(self, row: dict) -> str:
        """One-line human summary of a run, for the console and push
        notifications. The default is throughput-shaped (smoltcp-s3,
        linux-s3); a bench whose headline numbers aren't Gbps/workers/conns
        (duckdb-tpch's are ms/rows/match) should override this."""
        s = (
            f"{row.get('gbps')} Gbps, {row.get('workers_actual')} workers, "
            f"{row.get('conns_clean')}/{row.get('conns_total')} clean"
        )
        if row.get("setup_us_p50") is not None:
            s += f", setup p50 {row['setup_us_p50']} us"
        if row.get("dial_loop_ms") is not None:
            s += f", dial loop {row['dial_loop_ms']} ms"
        return s

    # -- shared -------------------------------------------------------------

    def live(self) -> list[str]:
        r = ec2().describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [self.instance_tag]},
                {"Name": "instance-state-name", "Values": ["pending", "running"]},
            ]
        )
        return [i["InstanceId"] for x in r["Reservations"] for i in x["Instances"]]

    def valid(self, row: dict) -> bool:
        """The baseline prints misrouted as 0 so this gate is shared.

        No syn_retries: the counter was hardcoded to 1 attempt and read 0 on
        every run. A lost SYN shows up in setup_us_max instead.
        """
        return bool(
            row.get("complete")
            and (row.get("setup_us_max") or 0) < 1_000_000
            and row.get("misrouted") == 0
            # `or 0`: an unreported field is None, and None == 0 is False.
            and (row.get("http_bad") or 0) == 0
            and row.get("conns_total") == row.get("conns_clean")
        )


def done(df: pd.DataFrame, axis: str, value, rep: int, cfg: dict) -> bool:
    """Whether the CSV already holds this run. Every knob counts, not just the
    axis: two points can share an axis value and differ elsewhere."""
    mask = (df.get("axis") == axis) & (df.get("axis_value") == value) & (df.get("rep") == rep)
    for k, v in cfg.items():
        if k != axis and k in df.columns:
            mask &= df[k].fillna("").astype(str) == str(v)
    return bool(mask.any())


def main(bench: Bench, argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=bench.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    axes = "/".join(bench.knobs)
    ap.add_argument(
        "--sweep", required=True, metavar="KNOB=V1,V2", help=f"axis to vary: {axes}"
    )
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--instance", default=bench.default_instance)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument(
        "--cooldown",
        type=int,
        default=600,
        metavar="SEC",
        help="idle time between runs. Unspaced reps are not independent: "
        "dial cost went 0.08 -> 20 ms/conn between otherwise identical reps",
    )
    ap.add_argument(
        "--interleave",
        action="store_true",
        help="rep-major (a/b/a/b) not value-major, so drift hits both arms",
    )
    ap.add_argument(
        "--keep-going",
        action="store_true",
        help="continue after an invalid run; the default abandons the queue",
    )
    ap.add_argument(
        "--max-vm-seconds",
        type=int,
        default=bench.max_vm_seconds,
        metavar="SEC",
        help="ceiling on one instance's life, not a delay; raise it when a "
        "point does more work per run",
    )
    ap.add_argument(
        "--target-ip",
        default=None,
        metavar="ADDR",
        help="S3 address to compile in; resolved per invocation when "
        "omitted, and front-ends do not perform alike",
    )
    ap.add_argument(
        "--market",
        choices=MARKETS,
        default="on-demand",
        help="spot is about a tenth of the price; 'spot' fails if one cannot "
        "be provided, 'spot-or-on-demand' falls back. A run reclaimed mid-way "
        "is an invalid row, not a retry",
    )
    ap.add_argument("--dry-run", action="store_true")
    for knob, (_env, parser) in bench.knobs.items():
        ap.add_argument(
            f"--{knob}",
            type=parser,
            default=None,
            help=f"fixed value for {knob} when it is not the axis",
        )
    bench.add_arguments(ap)
    a = ap.parse_args(argv)

    bench.max_vm_seconds = a.max_vm_seconds
    bench.market = a.market

    axis, _, raw = a.sweep.partition("=")
    if not raw:
        raise SystemExit("--sweep needs values, e.g. conns=1,2,4")
    if axis == INSTANCE_AXIS:
        values = [v.strip() for v in raw.split(",") if v.strip()]
    elif axis in bench.knobs:
        values = [bench.knobs[axis][1](v) for v in raw.split(",")]
    else:
        raise SystemExit(
            f"--sweep must be {INSTANCE_AXIS} or one of {list(bench.knobs)}"
        )

    # Same tree as the experiments: results/<axis>/<os>/. The transport is in
    # the name because http and https rows are not comparable.
    transport = "http" if os.environ.get("BENCH_SCHEME") == "http" else "tls"
    out = a.out or ROOT / f"results/{axis}/{bench.os_name}/adhoc-{transport}.csv"

    base = {}
    for knob, (_env, parser) in bench.knobs.items():
        v = getattr(a, knob)
        if v is None:
            v = bench.defaults.get(knob)
        base[knob] = parser(v) if isinstance(v, str) else v

    for req in ("AWS_BUCKET", "AWS_REGION"):
        if req not in os.environ:
            raise SystemExit(f"{req} missing — run `just setup` for this bench")

    plan = (
        [(v, r) for r in range(1, a.reps + 1) for v in values]
        if a.interleave
        else [(v, r) for v in values for r in range(1, a.reps + 1)]
    )

    def gib(v):
        c = {**base, axis: v}
        per_worker = c.get("blocks") or c.get("conns", 1)
        n = c.get("workers", 1) * per_worker * c.get("block", 0)
        return n / (1 << 30)

    print(
        f"bench    : {bench.name}"
        f"\ninstance : {'swept' if axis == INSTANCE_AXIS else a.instance}"
        f" ({a.market})"
        f"\nvm cap   : {a.max_vm_seconds}s per run"
        f"\ncooldown : {a.cooldown}s between runs"
        f"\naxis     : {axis} = {values}"
        f"\norder    : {'interleaved (rep-major)' if a.interleave else 'value-major'}"
        f"\nfixed    : {({k: v for k, v in base.items() if k != axis})}"
        f"\nruns     : {len(plan)} ({len(values)} builds x {a.reps} reps)"
        f"\ntransfer : up to {sum(gib(v) for v, _ in plan):.1f} GiB"
        f"\nout      : {out}"
    )
    if a.dry_run:
        for v, r in plan:
            print(f"  would run {axis}={v} rep={r}")
        return 0

    ip = a.target_ip or target_ip()
    print(f"target   : {ip}{'' if a.target_ip else ' (resolved)'}")
    out.parent.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(out) if out.exists() else pd.DataFrame()
    if not df.empty:
        print(f"resuming : {len(df)} rows present")

    built = None
    ran_one = False
    for idx, (value, rep) in enumerate(plan):
        # Sweeping instances leaves the build alone; every other axis rebuilds.
        on_instances = axis == INSTANCE_AXIS
        instance = value if on_instances else a.instance
        cfg = dict(base) if on_instances else {**base, axis: value}
        if not df.empty and done(df, axis, value, rep, cfg):
            continue
        if cfg != built:
            print(f"\n=== building {cfg} ===", flush=True)
            bench.build(cfg, ip)
            built = cfg

        if ran_one and a.cooldown:
            print(f"    cooling down {a.cooldown}s before the next run", flush=True)
            time.sleep(a.cooldown)

        print(f"--- {axis}={value} rep={rep} ---", flush=True)
        ran_one = True
        for attempt in range(6):  # the vCPU quota counts instances still shutting down
            try:
                row = bench.run_once(instance, out.parent / "logs", cfg, ip)
            except Exception as e:  # noqa: BLE001
                if "VcpuLimitExceeded" not in str(e) or attempt == 5:
                    raise
                row = None
            if row is not None and row.get("interrupted"):
                print("    WARN: EC2 reclaimed the spot instance mid-run", flush=True)
            log = out.parent / "logs" / str((row or {}).get("log") or "")
            if row is not None and (bench.valid(row) or not log.is_file()
                                    or "VcpuLimitExceeded" not in log.read_text(errors="replace")):
                break
            print("    vCPU quota hit; retrying in 90 s", flush=True)
            time.sleep(90)
        row |= {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "instance": instance,
            "axis": axis,
            "axis_value": value,
            "rep": rep,
            "note": os.environ.get("BENCH_NOTE", ""),
            **cfg,
        }
        row["valid"] = bench.valid(row)
        if row.get("bytes") and row.get("elapsed_s"):
            row["est_rx_pps"] = round(row["bytes"] / 1460 / row["elapsed_s"])
        if row.get("gbps") and row.get("workers_actual"):
            row["gbps_per_worker"] = round(row["gbps"] / row["workers_actual"], 4)
        if row.get("setup_us_p50"):
            # p50, not the mean: one slow handshake is not every handshake.
            row["setup_degraded"] = row["setup_us_p50"] > 10 * SETUP_BASELINE_US

        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        df.to_csv(out, index=False)  # a partial sweep survives interruption
        summary = bench.summary(row)
        print(f"  {summary}, valid={row['valid']}")
        notify(
            summary,
            title=f"[{idx + 1}/{len(plan)}] {axis}={value} rep {rep} "
            f"{'OK' if row['valid'] else 'INVALID'}",
            tags="white_check_mark" if row["valid"] else "warning",
        )

        if not row["valid"] and not a.keep_going:
            msg = (
                f"{axis}={value} rep={rep} invalid ({summary}, "
                f"complete={row.get('complete')}); abandoning "
                f"{len(plan) - idx - 1} queued runs"
            )
            print(f"\nSTOPPING: {msg}")
            notify(msg, title=f"{bench.name} sweep STOPPED", tags="rotating_light")
            break

    ran = df[df["axis"] == axis] if "axis" in df else df
    good = ran[ran["valid"] == True] if "valid" in ran else ran  # noqa: E712
    if len(good) and bench.headline_metric in good:
        agg = getattr(good[bench.headline_metric], bench.headline_agg)()
        best = f"{agg:.1f}"
    else:
        best = "n/a"
    where = ", ".join(values) if axis == INSTANCE_AXIS else a.instance
    print(f"\n{len(ran)} runs on {where}, {len(good)} valid, "
          f"best {best} {bench.headline_unit}")
    print(f"wrote {out} ({len(df)} rows)")
    return 0


def cli(bench: Bench) -> None:
    sys.exit(main(bench))
