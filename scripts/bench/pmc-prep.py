#!/usr/bin/env python3
"""Reshape raw pmc-* captures into a sweep CSV that plot.py can draw.

    just reproduce pmc-primitives

The pmc-cost/pmc-perfevent/pmc-sample benches each capture their own raw
per-boot CSV, named so the machine and system are encoded in the filename
rather than a column. This turns one of those capture sets into the same
axis/axis_value/valid/instance/value shape every other experiment's sweep
CSV already has, one row per raw measurement -- plot.py's own mean/min-max
aggregation over reps does the rest, so there is no pooling logic here to
keep in sync with it.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results"

# The workload size the counting figure reports at. The cost is flat in
# region size -- measured from 1ns to 74us, it never moves off its constant --
# so a sweep would spend a whole axis proving a line is horizontal.
COUNTING_KEYS = 2000

# Nitro grants a guest only 2 counters on Graviton (measured on c7g.large and
# c8g.large alike) against 8 on c7i and 5 on c7a, so these are the three
# machines with a same-generation, same-counter-budget pair to compare.
MACHINES = ["c7i.large", "c7a.large", "c7g.large"]

# loop_overhead is the sub-nanosecond calibration floor the others sit on,
# not a primitive worth plotting next to them.
PRIMS = ["pmc_start_with_conf", "pmc_read", "pmc_write", "pmc_stop"]


def run_id(csv: Path) -> tuple[str, str]:
    stem = csv.stem.removesuffix("-perfevent")
    _, _, rest = stem.partition("-")
    if rest.startswith("linux-"):
        return "Linux", rest[len("linux-"):]
    return "miniOSv", rest


def sample_id(csv: Path) -> tuple[str, str]:
    stem = csv.stem.removesuffix("-sample")
    _, _, rest = stem.partition("-")
    if rest.startswith("linuxnt-"):
        return "Linux (no throttle)", rest[len("linuxnt-"):]
    if rest.startswith("linux-"):
        return "Linux", rest[len("linux-"):]
    return "miniOSv", rest


def clock_of(log: Path) -> float:
    """Core clock in MHz from a capture's console log, NaN if absent."""
    if not log.exists():
        return float("nan")
    m = re.search(r"cpu_mhz=([0-9.]+)", log.read_text(errors="ignore"))
    return float(m.group(1)) if m else float("nan")


def counting() -> pd.DataFrame:
    rows = []
    for csv in sorted(RESULTS.glob("pmc-perfevent/*-perfevent.csv")):
        if not csv.stat().st_size:
            continue
        system, machine = run_id(csv)
        d = pd.read_csv(csv)
        for ns in d[d["keys"] == COUNTING_KEYS]["delta_ns"]:
            # Added time cannot be negative; when it reads that way, a clock
            # or scheduler artifact hit one of the two reads around the
            # region, not the region itself.
            rows.append(dict(axis="machine", axis_value=machine, value=ns / 1e3,
                              valid=ns >= 0, instance=machine, series=system))
    return pd.DataFrame(rows)


def primitives() -> pd.DataFrame:
    rows = []
    for machine in MACHINES:
        for csv in sorted(RESULTS.glob(f"pmc-cost/*-{machine}-prim.csv")):
            d = pd.read_csv(csv)
            for op in PRIMS:
                for ns in d[d["op"] == op]["ns_per_op"]:
                    rows.append(dict(axis="op", axis_value=op, value=ns / 1e3,
                                      valid=ns >= 0, instance=machine, series=machine))
    return pd.DataFrame(rows)


def metal() -> pd.DataFrame:
    """The same primitives with no hypervisor underneath, in cycles rather
    than nanoseconds since nothing manages P-states on bare metal.

    c7i.metal-24xl against c7i.large: same Sapphire Rapids core, same kernel,
    same image, only the hypervisor differs.
    """
    src = RESULTS / "pmc-cost" / "metal-experiment"
    rows = []
    for pattern, label in (("*c7i.metal-24xl-prim.csv", "Bare metal"),
                            ("*c7i.large-prim.csv", "Virtualized")):
        for csv in sorted(src.glob(pattern)):
            mhz = clock_of(csv.with_name(csv.name.replace("-prim.csv", ".log")))
            if not csv.stat().st_size or mhz != mhz:
                continue
            d = pd.read_csv(csv)
            for op in PRIMS:
                for ns in d[d["op"] == op]["ns_per_op"]:
                    rows.append(dict(axis="op", axis_value=op, value=ns * mhz / 1e3,
                                      valid=ns >= 0, instance="c7i", series=label))
    return pd.DataFrame(rows)


def sampling() -> pd.DataFrame:
    rows = []
    for csv in sorted(RESULTS.glob("pmc-sample/*-sample.csv")):
        if not csv.stat().st_size:
            continue
        d = pd.read_csv(csv)
        if not {"freq_hz", "base_ns", "sampled_ns", "dead"} <= set(d.columns):
            continue
        # A rep that armed but never fired is broken, not cheap.
        d = d[(d["dead"] == 0) & (d["base_ns"] > 0)]
        system, inst = sample_id(csv)
        for freq, base, sampled in zip(d["freq_hz"], d["base_ns"], d["sampled_ns"]):
            overhead_pct = 100 * (sampled - base) / base
            rows.append(dict(axis="freq_hz", axis_value=freq, value=overhead_pct,
                              valid=True, instance=inst, series=f"{system} {inst}"))
    return pd.DataFrame(rows)


FIGURES = dict(counting=counting, primitives=primitives, metal=metal, sampling=sampling)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("figure", choices=sorted(FIGURES))
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    df = FIGURES[a.figure]()
    if df.empty:
        raise SystemExit(f"no usable captures for {a.figure!r} under {RESULTS}")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(a.out, index=False)
    print(f"wrote {a.out} ({len(df)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
