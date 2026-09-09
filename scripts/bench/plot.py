#!/usr/bin/env python3
"""Plot a sweep CSV: some measured value against whichever knob varied.

    just plot smoltcp-s3
    just plot results/x/sweep-workers.csv --dark
    just plot results/pmc-cost/primitives.csv --value-col value --series machine

The CSV records which knob was the axis, so one code path draws every sweep:
throughput against connection count, or microseconds against a primitive
name, or anything else with an axis / axis_value / valid / instance shape.
--series splits into one line per value of a column.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

# Fixed slots, never cycled. Checked with a CVD simulator (tab10's default
# green-on-orange is the classic red-green confusion).
INK = {
    "light": dict(surface="#fcfcfb", text="#0b0b0b", muted="#898781",
                  grid="#e1e0d9", axis="#c3c2b7", bad="#d03b3b",
                  series=["#2a78d6", "#eb6834", "#1baf7a"]),
    "dark": dict(surface="#1a1a19", text="#ffffff", muted="#898781",
                 grid="#2c2c2a", axis="#383835", bad="#d03b3b",
                 series=["#3987e5", "#d95926", "#199e70"]),
}

# Shape, not just hue, so a series still reads in greyscale.
MARKERS = ["o", "s", "^"]
DASHES = ["-", "--", ":"]
HATCH = ["", "//", "xx"]


def rc(c: dict) -> dict:
    """The chrome, as rcParams rather than a call per element."""
    return {
        "figure.facecolor": c["surface"], "savefig.facecolor": c["surface"],
        "axes.facecolor": c["surface"], "axes.edgecolor": c["axis"],
        "axes.linewidth": 0.8, "axes.labelcolor": c["text"],
        "axes.labelsize": 9.5, "axes.titlesize": 12, "axes.titlecolor": c["text"],
        "axes.titlelocation": "left", "axes.titlepad": 16,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "axes.grid.axis": "y", "axes.axisbelow": True,
        "grid.color": c["grid"], "grid.linewidth": 0.8,
        "text.color": c["text"], "font.size": 9,
        "xtick.color": c["muted"], "ytick.color": c["muted"],
        "xtick.labelsize": 9, "ytick.labelsize": 9,
        "xtick.major.size": 0, "ytick.major.size": 0,
        "legend.frameon": True, "legend.fontsize": 9,
        "legend.facecolor": c["surface"], "legend.edgecolor": c["axis"],
        "legend.framealpha": 1,
    }

LABELS = {
    "conns": "Concurrent connections per worker",
    "workers": "Worker threads (RSS queues)",
    "block": "Block size per request",
    "instance": "EC2 instance size",
}

# EC2's sustained (non-burst) allowance in Gbps, from describe-instance-types.
CEILING = {
    "c6in.large": 3.125,
    "c6in.xlarge": 6.25,
    "c6in.2xlarge": 12.5,
    "c6in.4xlarge": 25.0,
    "c6in.8xlarge": 50.0,
    "c6in.16xlarge": 100.0,
    "c7i.8xlarge": 12.5,
    "c7i.large": 12.5,
}


def numeric(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def rank(axis: str, v, order: dict | None = None) -> float:
    """Sort key: instance sizes by machine size, numbers by value, anything
    else by first appearance in the CSV."""
    if axis == "instance":
        size = str(v).rsplit(".", 1)[-1]
        if size == "large":
            return 1.0
        if size == "xlarge":
            return 2.0
        return float(size.removesuffix("xlarge")) * 2
    if numeric(v):
        return float(v)
    return float(order[v]) if order else 0.0


def tick(axis: str, v) -> str:
    if axis == "instance":
        return str(v).split(".", 1)[-1]
    if axis == "block":
        return f"{int(v) >> 20}M"
    return f"{float(v):g}" if numeric(v) else str(v)


def summarise(df: pd.DataFrame, series_col: str | None, value_col: str):
    """One row per (series, axis value): mean over valid reps, plus spread."""
    keys = ([series_col] if series_col else []) + ["axis_value"]
    g = df[df["valid"]].groupby(keys)[value_col]
    out = g.agg(mean="mean", lo="min", hi="max", n="count").reset_index()
    axis = str(df["axis"].iloc[0])
    order = {v: i for i, v in enumerate(dict.fromkeys(df["axis_value"]))}
    return out.sort_values("axis_value", key=lambda c: c.map(lambda v: rank(axis, v, order)))


def plot(
    df: pd.DataFrame,
    out: Path,
    mode: str = "light",
    series_col: str | None = None,
    title: str | None = None,
    value_col: str = "gbps",
    ylabel: str = "Throughput (Gbps)",
    unit: str = "Gbps",
    bar: bool = False,
    log_scale: bool = False,
) -> None:
    c = INK[mode]
    axis = str(df["axis"].iloc[0])
    instance = str(df["instance"].iloc[0])
    stats = summarise(df, series_col, value_col)
    order = sorted(stats["axis_value"].unique(),
                    key=lambda v: rank(axis, v, {v: i for i, v in
                                                  enumerate(dict.fromkeys(df["axis_value"]))}))
    # Named axes (categories, or any bar chart) are placed evenly, not by value.
    named = bar or axis == "instance" or not all(numeric(v) for v in order)
    at = {v: i for i, v in enumerate(order)}
    xof = (lambda col: col.map(at)) if named else (lambda col: col)
    groups = (
        [(k, g) for k, g in stats.groupby(series_col)]
        if series_col
        else [(None, stats)]
    )

    plt.rcParams.update(rc(c))
    fig, ax = plt.subplots(figsize=(8, 4.8), dpi=160)

    ceiling = None
    if value_col == "gbps":
        if named:
            # Each point has its own allowance, so the ceiling is a line.
            ceilings = [CEILING.get(v) for v in order]
            ceiling = max([x for x in ceilings if x], default=0) or None
            if any(ceilings):
                ax.plot(
                    range(len(order)),
                    ceilings,
                    color=c["muted"],
                    lw=1,
                    ls=(0, (5, 4)),
                    marker="",
                    zorder=1,
                    label="EC2 sustained allowance",
                )
        else:
            ceiling = CEILING.get(instance)
            if ceiling:
                ax.axhline(ceiling, color=c["muted"], lw=1, ls=(0, (5, 4)), zorder=1)
                ax.annotate(
                    f"{instance} sustained, {ceiling:g} Gbps",
                    (0.995, ceiling),
                    xycoords=ax.get_yaxis_transform(),
                    textcoords="offset points",
                    xytext=(0, 5),
                    ha="right",
                    fontsize=8,
                    color=c["muted"],
                )

    # Latin square (hue = i%n, shape = (i+i//n)%n): a repeated hue never
    # repeats its earlier shape.
    n_hue = len(c["series"])
    if bar:
        width = 0.8 / len(groups)
        for i, (name, g) in enumerate(groups):
            col = c["series"][i % n_hue]
            shape = (i + i // n_hue) % n_hue
            pos = xof(g["axis_value"]) + (i - (len(groups) - 1) / 2) * width
            err = [(g["mean"] - g["lo"]).tolist(), (g["hi"] - g["mean"]).tolist()]
            ax.bar(pos, g["mean"], width, yerr=err, capsize=3, color=col,
                   hatch=HATCH[shape % len(HATCH)],
                   edgecolor=c["surface"], linewidth=0.8, label=name, zorder=3)
            # Above the whisker (hi), not the bar -- else the label sits
            # inside the yerr line.
            for xi, v, hi in zip(pos, g["mean"], g["hi"]):
                ax.annotate(f"{v:.1f}", (xi, hi), textcoords="offset points",
                            xytext=(0, 3), ha="center", fontsize=7.5, zorder=4)
    else:
        for i, (name, g) in enumerate(groups):
            col = c["series"][i % n_hue]
            shape = (i + i // n_hue) % n_hue
            ax.fill_between(
                xof(g["axis_value"]), g["lo"], g["hi"], color=col, alpha=0.18, lw=0, zorder=2
            )
            ax.plot(
                xof(g["axis_value"]),
                g["mean"],
                color=col,
                ls=DASHES[shape],
                lw=2,
                marker=MARKERS[shape],
                ms=6,
                mec=c["surface"],
                mew=1.5,
                label=name,
                zorder=3,
            )

    bad = df[~df["valid"]]
    if len(bad):
        ax.scatter(
            xof(bad["axis_value"]),
            bad[value_col],
            marker="x",
            s=55,
            color=c["bad"],
            lw=1.8,
            zorder=4,
        )
        ax.scatter(
            [],
            [],
            marker="x",
            s=55,
            color=c["bad"],
            lw=1.8,
            label="invalid run (excluded)",
        )

    if not bar:
        # Bars already label every value; this is the line case's only one.
        best = stats.loc[stats["mean"].idxmax()]
        ax.annotate(
            f"{best['mean']:.1f} {unit}",
            (at[best["axis_value"]] if named else best["axis_value"], best["mean"]),
            textcoords="offset points",
            xytext=(0, 11),
            ha="center",
            fontweight="medium",
        )

    if not named:
        ax.set_xscale("log", base=2)
    xs = list(range(len(order))) if named else order
    ax.set_xticks(xs)
    ax.set_xticklabels([tick(axis, v) for v in order])
    ax.minorticks_off()
    ax.margins(x=0.06)  # room for the end labels
    if log_scale:
        ax.set_yscale("log")
    else:
        # Anchored at zero: cropping a magnitude's baseline exaggerates slope.
        ax.set_ylim(0, max(ceiling or 0, stats["hi"].max()) * 1.12)

    ax.set_xlabel(LABELS.get(axis, axis))
    ax.set_ylabel(ylabel)
    reps = int(stats["n"].max())
    fixed = ", ".join(
        f"{k}={tick(k, df[k].iloc[0])}" for k in LABELS if k in df and k != axis
    )
    ax.set_title(title or f"{ylabel.split(' (')[0]} vs {axis}")
    ax.text(
        0,
        1.02,
        (f"{fixed} · mean of {reps} runs, band is min–max" if named
         else f"{instance} · {fixed} · mean of {reps} runs, band is min–max"),
        transform=ax.transAxes,
        fontsize=8.5,
        color=c["muted"],
    )

    if ax.get_legend_handles_labels()[0]:
        ax.legend(labelcolor=c["text"])

    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "csv",
        type=Path,
        nargs="+",
        help="path to a sweep CSV, or a bench name whose "
        "results/<name>/sweep.csv is used. Several are concatenated, so two "
        "stacks' sweeps overlay in one figure with --series note",
    )
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--dark", action="store_true")
    ap.add_argument(
        "--series", default=None, help="column to split into one line per value"
    )
    ap.add_argument("--title", default=None)
    ap.add_argument(
        "--value-col", default="gbps", help="CSV column to plot on the y-axis"
    )
    ap.add_argument("--ylabel", default="Throughput (Gbps)")
    ap.add_argument(
        "--unit", default="Gbps", help="unit suffix for the peak-value annotation"
    )
    ap.add_argument(
        "--bar", action="store_true",
        help="grouped bars instead of connected markers",
    )
    ap.add_argument(
        "--log-scale", action="store_true", help="log y-axis"
    )
    a = ap.parse_args()

    root = Path(__file__).resolve().parents[2]
    csvs = []
    for p in a.csv:
        csv = p if p.is_file() else root / "results" / p.name / "sweep.csv"
        if not csv.is_file():
            raise SystemExit(f"no such sweep CSV: {p}")
        csvs.append(csv)
    a.csv = csvs[0]
    df = pd.concat([pd.read_csv(c) for c in csvs], ignore_index=True)
    if df.empty:
        raise SystemExit(f"{csvs[0]} has no rows")
    mode = "dark" if a.dark else "light"
    out = a.out or a.csv.with_name(f"{a.csv.stem}{'-dark' if a.dark else ''}.png")
    plot(df, out, mode, a.series, a.title, a.value_col, a.ylabel, a.unit,
         a.bar, a.log_scale)
    valid = int(df["valid"].sum())
    print(f"wrote {out} ({valid}/{len(df)} runs valid)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
