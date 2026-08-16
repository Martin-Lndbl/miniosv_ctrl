#!/usr/bin/env python3
"""Run a stored experiment end to end: build, sweep, plot.

    just reproduce conns-plateau
    just reproduce worker-scaling --dry-run

An experiment is a TOML file under experiments/ holding every parameter that
decides what the numbers mean, plus prose saying what it measures and why.
Reproducing one should need nothing but its name.

Each [[points]] entry is one configuration, run as its own sweep invocation
into a shared CSV -- the resume key is (axis, axis_value, rep), so points
accumulate rather than collide. Points are separate invocations because a knob
other than the axis may co-vary with it: worker-scaling derives conns from
workers to hold total connections constant, which a single --sweep cannot say.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import runner  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS = ROOT / "experiments"

# Written by `just setup <bench>`; not derivable, so we refuse rather than guess.
REQUIRED_ENV = ("AWS_REGION", "AWS_BUCKET", "AWS_SUBNET")


def load(name: str) -> dict:
    path = Path(name) if Path(name).is_file() else EXPERIMENTS / f"{name}.toml"
    if not path.is_file():
        known = ", ".join(sorted(p.stem for p in EXPERIMENTS.glob("*.toml")))
        raise SystemExit(f"no such experiment: {name} (have: {known})")
    return tomllib.loads(path.read_text()) | {"path": path}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("experiment")
    ap.add_argument(
        "--cooldown",
        type=int,
        default=None,
        metavar="SEC",
        help="idle time between reps of a point; overrides the "
        "experiment's own value",
    )
    ap.add_argument(
        "--point-cooldown",
        type=int,
        default=None,
        metavar="SEC",
        help="idle time between points; defaults to --cooldown",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-plot", action="store_true")
    a = ap.parse_args()

    x = load(a.experiment)
    axis, points = x["axis"], x["points"]
    out = ROOT / x["out"]
    driver = ROOT / "scripts/bench" / Path(x["bench"]).name / "bench.py"
    # Two distinct waits: between reps of one point (runner's own), and between
    # points here. They default to the same value but need not be equal — the
    # gap between points already absorbs a rebuild.
    cooldown = a.cooldown if a.cooldown is not None else x.get("cooldown", 600)
    point_cooldown = (
        a.point_cooldown
        if a.point_cooldown is not None
        else x.get("point_cooldown", cooldown)
    )

    print(f"experiment : {x['name']}\n{x['description'].strip()}\n")
    print(
        f"instance   : {x['instance']}\naxis       : {axis}"
        f"\npoints     : {len(points)} x {x['reps']} reps"
        f"\nvm cap     : {x.get('max_vm_seconds', 110)}s per run"
        f"\ncooldown   : {cooldown}s between reps, {point_cooldown}s between "
        f"points\nout        : {out}"
    )

    if missing := [k for k in REQUIRED_ENV if k not in os.environ]:
        raise SystemExit(
            f"{', '.join(missing)} missing — run "
            f"`just setup {x['bench']}` once, then retry"
        )

    env = {**os.environ, **{k: str(v) for k, v in x.get("env", {}).items()}}
    fixed = x.get("fixed", {})
    # Once for the whole experiment: S3 front-ends do not perform alike, and one
    # sweep spread over three showed 29-30 Gbps on one and 18 on another.
    ip = x.get("target_ip") or runner.target_ip()
    print(
        f"target     : {ip}"
        f"{' (pinned in the experiment)' if x.get('target_ip') else ' (resolved once)'}"
    )

    for i, point in enumerate(points):
        cfg = {**fixed, **point}
        held = [s for k, v in cfg.items() if k != axis for s in (f"--{k}", str(v))]
        cmd = [
            sys.executable,
            str(driver),
            "--sweep",
            f"{axis}={point[axis]}",
            "--reps",
            str(x["reps"]),
            "--instance",
            x["instance"],
            "--cooldown",
            str(cooldown),
            "--max-vm-seconds",
            str(x.get("max_vm_seconds", 110)),
            "--target-ip",
            ip,
            "--out",
            str(out),
            *held,
        ]
        if a.dry_run:
            cmd.append("--dry-run")

        print(f"\n=== point {i + 1}/{len(points)}: {cfg} ===", flush=True)
        r = subprocess.run(cmd, cwd=ROOT, env=env)
        if r.returncode:
            raise SystemExit(f"point {cfg} failed with {r.returncode}")

        # runner cools down between reps within a point, not across points.
        if not a.dry_run and i + 1 < len(points) and point_cooldown:
            print(
                f"    cooling down {point_cooldown}s before the next point", flush=True
            )
            time.sleep(point_cooldown)

    if a.dry_run or a.no_plot:
        return 0
    # Loud but not fatal: the runs are already in the CSV, so a plotting fault
    # must not read as a failed experiment — nor pass silently after hours.
    r = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/bench/plot.py"),
            str(out),
            "--title",
            x["title"],
        ],
        cwd=ROOT,
    )
    if r.returncode:
        print(
            f"WARN: data is in {out} but plotting failed; rerun with "
            f"`just plot {out.relative_to(ROOT)}`",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
