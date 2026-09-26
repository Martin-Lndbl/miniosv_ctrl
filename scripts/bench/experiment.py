#!/usr/bin/env python3
"""Run a stored experiment end to end: build, sweep, plot.

    just reproduce miniosv-tls-conns
    just reproduce miniosv-tls-workers --dry-run --reps 1

An experiment is a TOML file under experiments/<subject>/; rows go to
results/<subject>/<name>.csv. Each [[points]] entry is its own sweep
invocation into that CSV, keyed by (axis, axis_value, rep).
"""

from __future__ import annotations

import argparse
import itertools
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

REQUIRED_ENV = ("AWS_REGION", "AWS_BUCKET", "AWS_SUBNET")


def qualified(path: Path) -> str:
    return str(
        path.relative_to(EXPERIMENTS).with_suffix("")
        if path.is_relative_to(EXPERIMENTS)
        else path
    )


def find(name: str) -> Path:
    """`s3/miniosv-tls-workers`, a bare `miniosv-tls-workers`, or a path."""
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
    """results/<subject>/<name>.csv, derived from the file's path."""
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
    # `grid = { query = [1, 6], threads = [32, 64] }` is the cartesian product;
    # `[[points]]` remains for knobs that co-vary.
    if grid := x.get("grid"):
        x["points"] = [dict(zip(grid, v)) for v in itertools.product(*grid.values())]
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
    ap.add_argument("--reps", type=int, default=None, help="overrides the experiment's value")
    ap.add_argument(
        "--market",
        choices=runner.MARKETS,
        default="spot",
        help="where the machines come from; the default is spot in any zone "
        "of the VPC, and the experiment fails if no zone has one",
    )
    ap.add_argument(
        "--only",
        default=None,
        metavar="KNOB=VALUE",
        help="run just the points where KNOB has this value, e.g. query=3; "
        "how `just queue --interleave` alternates arms per point",
    )
    ap.add_argument(
        "--target-ip",
        default=None,
        metavar="ADDR",
        help="the S3 front-end to use, over the experiment's own choice; the "
        "queue resolves one per point so the arms of a comparison share it",
    )
    runner.add_profile_arg(ap)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-plot", action="store_true")
    a, extra = ap.parse_known_args()
    runner.apply_profile(a.aws_profile)

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
    if a.only:
        knob, _, value = a.only.partition("=")
        points = [p for p in points if str(p.get(knob, x.get("fixed", {}).get(knob))) == value]
        if not points:
            raise SystemExit(f"--only {a.only} matches no point of {qualified(x['path'])}")
    reps = a.reps if a.reps is not None else x["reps"]
    driver = ROOT / "scripts/bench" / Path(x["bench"]).name / "bench.py"
    cooldown = a.cooldown if a.cooldown is not None else x.get("cooldown", 600)
    point_cooldown = (
        a.point_cooldown
        if a.point_cooldown is not None
        else x.get("point_cooldown", cooldown)
    )

    print(f"experiment : {qualified(x['path'])}\n{x['description'].strip()}\n")
    print(
        f"instance   : {x['instance']} ({a.market})"
        f"\naxis       : {axis}"
        f"\npoints     : {len(points)} x {reps} reps"
        f"\nvm cap     : {x.get('max_vm_seconds', 110)}s per run"
        f"\ncooldown   : {cooldown}s between reps, {point_cooldown}s between "
        f"points\nout        : {out}"
    )

    if missing := [k for k in REQUIRED_ENV if k not in os.environ]:
        raise SystemExit(
            f"{', '.join(missing)} missing — run "
            f"`just setup {x['bench']}` once, then retry"
        )

    if not a.dry_run:
        runner.check_bucket_region()

    fixed = x.get("fixed", {})
    # Once per experiment: S3 front-ends do not perform alike.
    ip = a.target_ip or x.get("target_ip") or runner.target_ip()
    print(
        f"target     : {ip}"
        f"{' (given)' if a.target_ip else ' (pinned in the experiment)' if x.get('target_ip') else ' (resolved once)'}"
    )

    # Interleaved: one rep-major invocation per group of points that share
    # every knob but the axis, so S3 drift lands on every value alike.
    if x.get("interleave"):
        groups: dict = {}
        for p in points:
            key = tuple(sorted((k, v) for k, v in p.items() if k != axis))
            groups.setdefault(key, []).append(p[axis])
        points = [dict(key) | {axis: ",".join(map(str, vs))} for key, vs in groups.items()]

    for i, point in enumerate(points):
        cfg = {**fixed, **point}
        # A note may name the point: BENCH_NOTE = "miniOSv, {threads} threads".
        env = {**os.environ, **{k: str(v).format(**cfg) for k, v in x.get("env", {}).items()}}
        instance = str(cfg.pop("instance", x["instance"])) if axis != "instance" else x["instance"]
        held = [s for k, v in cfg.items() if k != axis for s in (f"--{k}", str(v))]
        cmd = [
            sys.executable,
            str(driver),
            "--sweep",
            f"{axis}={point[axis]}",
            "--reps",
            str(reps),
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
        if x.get("keep_going"):
            cmd.append("--keep-going")
        if x.get("interleave"):
            cmd.append("--interleave")
        cmd += ["--market", a.market]
        if a.dry_run:
            cmd.append("--dry-run")

        print(f"\n=== point {i + 1}/{len(points)}: {cfg} ===", flush=True)
        r = subprocess.run(cmd, cwd=ROOT, env=env)
        if r.returncode:
            raise SystemExit(f"point {cfg} failed with {r.returncode}")

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
