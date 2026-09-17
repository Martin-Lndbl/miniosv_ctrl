#!/usr/bin/env python3
"""Run a stored experiment end to end: build, sweep, plot.

    just reproduce miniosv-tls-conns
    just reproduce miniosv-tls-workers --dry-run

An experiment is a TOML file under experiments/ holding every parameter that
decides what the numbers mean, plus prose saying what it measures and why.
Reproducing one should need nothing but its name.

They are grouped by subject -- experiments/s3, experiments/tpch, experiments/pmc
-- not by the knob they sweep, which is a property of a run rather than an
identity: grouping by axis put the four PMC experiments in three directories
and gave three unrelated files the same name. Rows go to
results/<subject>/<name>.csv, derived from the file's own path.

Each [[points]] entry is one configuration, run as its own sweep invocation
into a shared CSV; the resume key (axis, axis_value, rep) keeps them apart.
Separate invocations because a knob other than the axis may co-vary with it,
which a single --sweep cannot express.
"""

from __future__ import annotations

import argparse
import os
import shlex
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


def qualified(path: Path) -> str:
    return str(
        path.relative_to(EXPERIMENTS).with_suffix("")
        if path.is_relative_to(EXPERIMENTS)
        else path
    )


def find(name: str) -> Path:
    """`s3/miniosv-tls-workers`, a bare `miniosv-tls-workers`, or a path.

    Names carry their axis now, so bare ones are unique in practice -- but the
    ambiguity check stays: it is what would catch two subjects growing a file
    with the same name, which is exactly how three `miniosv-tls.toml` came to
    exist under the old layout."""
    for candidate in (Path(name), EXPERIMENTS / f"{name}.toml"):
        if candidate.is_file():
            return candidate

    hits = sorted(EXPERIMENTS.rglob(f"{name}.toml"))
    if len(hits) > 1:
        where = "\n  ".join(qualified(p) for p in hits)
        raise SystemExit(f"{name} names {len(hits)} experiments:\n  {where}")
    if not hits:
        known = "\n  ".join(qualified(p) for p in sorted(EXPERIMENTS.rglob("*.toml")))
        raise SystemExit(f"no such experiment: {name}\nhave:\n  {known}")
    return hits[0]


def out_path(x: dict) -> Path:
    """Where this experiment's rows land, derived from where its file sits.

    Deliberately not a TOML key. A hand-written path drifts from the layout,
    and four of them had: `freq_hz/pmc-sampling.toml` was filling
    `results/pmc-sample/sampling.csv`, so neither the directory nor the stem
    matched. Derived, moving an experiment is a rename and nothing else, and
    the file holding a run's numbers can always be found from its name.
    """
    path: Path = x["path"]
    return ROOT / "results" / path.parent.name / f"{path.stem}.csv"


def load(name: str) -> dict:
    path = find(name)
    x = tomllib.loads(path.read_text()) | {"path": path}
    if "out" in x:
        raise SystemExit(
            f"{qualified(path)} sets `out`, which is now derived from its "
            f"path ({out_path(x).relative_to(ROOT)}). Drop the key; to write "
            f"somewhere else, call the bench driver directly."
        )
    # Directories group by subject, not by axis, so there is no layout
    # invariant left to check -- only that a sweep says what it sweeps.
    if not ({"deploy", "prep"} & x.keys()) and not ({"axis", "points"} <= x.keys()):
        raise SystemExit(f"{qualified(path)} is a sweep but has no `axis`/`points`")
    return x


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
        help="idle time between reps; overrides the experiment's value",
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
    # A deploy experiment forwards extra args straight to its own justfile.
    a, extra = ap.parse_known_args()

    x = load(a.experiment)

    if deploy := x.get("deploy"):
        return remote(x, deploy, extra, a.dry_run)
    if extra:
        raise SystemExit(f"unexpected extra arguments: {extra}")

    out = out_path(x)
    out.parent.mkdir(parents=True, exist_ok=True)

    if prep := x.get("prep"):
        return local(x, prep, out, a.dry_run, a.no_plot)

    axis, points = x["axis"], x["points"]
    driver = ROOT / "scripts/bench" / Path(x["bench"]).name / "bench.py"
    # Between reps (runner's own) and between points (here); the latter
    # already absorbs a rebuild, so they need not be equal.
    cooldown = a.cooldown if a.cooldown is not None else x.get("cooldown", 600)
    point_cooldown = (
        a.point_cooldown
        if a.point_cooldown is not None
        else x.get("point_cooldown", cooldown)
    )

    # Qualified, so the line says which subject this belongs to as well.
    print(f"experiment : {qualified(x['path'])}\n{x['description'].strip()}\n")
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
        # `instance` is a runner parameter, not a knob: pass the point's own
        # machine when the axis is something else, and let --sweep carry it when
        # it is the axis.
        instance = str(cfg.pop("instance", x["instance"])) if axis != "instance" else x["instance"]
        held = [s for k, v in cfg.items() if k != axis for s in (f"--{k}", str(v))]
        cmd = [
            sys.executable,
            str(driver),
            "--sweep",
            f"{axis}={point[axis]}",
            "--reps",
            str(x["reps"]),
            "--instance",
            instance,
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
        # On the Linux baseline most points are allowance-shaped, which is the
        # result, not a defect that makes the rest of the queue pointless.
        if x.get("keep_going"):
            cmd.append("--keep-going")
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
    return run_plot(x, out)


def run_plot(x: dict, out: Path) -> int:
    cmd = [sys.executable, str(ROOT / "scripts/bench/plot.py"), str(out),
           "--title", x["title"]]
    for key, flag in (("series", "--series"), ("value_col", "--value-col"),
                       ("ylabel", "--ylabel"), ("unit", "--unit")):
        if key in x:
            cmd += [flag, str(x[key])]
    for key, flag in (("bar", "--bar"), ("log_scale", "--log-scale"),
                       ("box", "--box")):
        if x.get(key):
            cmd.append(flag)
    # Loud but not fatal: the data is already on disk.
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode:
        print(
            f"WARN: data is in {out} but plotting failed; rerun with "
            f"`just plot {out.relative_to(ROOT)}`",
            flush=True,
        )
    return 0


def remote(x: dict, deploy: str, extra: list[str], dry_run: bool) -> int:
    """Hands off to the bench's own justfile, which owns its boot loop."""
    print(f"experiment : {qualified(x['path'])}\n{x['description'].strip()}\n")
    bench_dir = ROOT / deploy
    cmd = ["just", "--justfile", str(bench_dir / "justfile"),
           "--working-directory", str(bench_dir), "reproduce", *extra]
    print(f"$ {shlex.join(cmd)}")
    if dry_run:
        return 0
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode:
        raise SystemExit(f"deploy failed with {r.returncode}")
    return 0


def local(x: dict, prep: str, out: Path, dry_run: bool, no_plot: bool) -> int:
    """Reshapes already-captured results instead of running a new sweep."""
    print(f"experiment : {qualified(x['path'])}\n{x['description'].strip()}\n")
    print(f"prep       : {prep}\nout        : {out}")
    if dry_run:
        return 0
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/bench/pmc-prep.py"), prep,
         "--out", str(out)],
        cwd=ROOT,
    )
    if r.returncode:
        raise SystemExit(f"prep {prep!r} failed with {r.returncode}")
    if no_plot:
        return 0
    return run_plot(x, out)


if __name__ == "__main__":
    raise SystemExit(main())
