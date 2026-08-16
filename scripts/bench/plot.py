#!/usr/bin/env python3
"""Plot a sweep CSV: throughput against whichever knob varied.

    just plot smoltcp-s3
    just plot results/smoltcp-s3/sweep-workers.csv --dark

The CSV records which knob was the axis, so one code path draws every sweep.
--series splits into one line per value of a column, for comparing stacks once
competitors/linux-s3 lands. A .md table is written beside the .png so no value
is reachable only by reading pixels.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt    # noqa: E402
import pandas as pd                # noqa: E402

# Reference palette. Series slots are assigned in fixed order, never cycled.
INK = {
    "light": dict(surface="#fcfcfb", text="#0b0b0b", muted="#898781",
                  grid="#e1e0d9", axis="#c3c2b7", bad="#d03b3b",
                  series=["#2a78d6", "#eb6834", "#1baf7a"]),
    "dark": dict(surface="#1a1a19", text="#ffffff", muted="#898781",
                 grid="#2c2c2a", axis="#383835", bad="#d03b3b",
                 series=["#3987e5", "#d95926", "#199e70"]),
}

LABELS = {"conns": "Concurrent connections per worker",
          "workers": "Worker threads (RSS queues)",
          "block": "Block size per request"}

# EC2's sustained allowance: the ceiling the curve is actually measured against.
CEILING = {"c6in.8xlarge": 50.0, "c7i.8xlarge": 12.5, "c7i.large": 12.5}


def tick(axis: str, v: float) -> str:
    return f"{int(v) >> 20}M" if axis == "block" else f"{int(v):g}"


def summarise(df: pd.DataFrame, series_col: str | None):
    """One row per (series, axis value): mean over valid reps, plus spread."""
    keys = ([series_col] if series_col else []) + ["axis_value"]
    g = df[df["valid"]].groupby(keys)["gbps"]
    out = g.agg(mean="mean", lo="min", hi="max", n="count").reset_index()
    return out.sort_values("axis_value")


def markdown(rows: list[list[str]], head: list[str]) -> str:
    w = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    line = lambda r: "| " + " | ".join(v.ljust(w[i]) for i, v in enumerate(r)) + " |"
    return "\n".join([line(head), "|" + "|".join("-" * (n + 2) for n in w) + "|",
                      *map(line, rows)])


def plot(df: pd.DataFrame, out: Path, mode: str = "light",
         series_col: str | None = None, title: str | None = None) -> Path:
    c = INK[mode]
    axis = str(df["axis"].iloc[0])
    instance = str(df["instance"].iloc[0])
    stats = summarise(df, series_col)
    groups = ([(k, g) for k, g in stats.groupby(series_col)] if series_col
              else [(None, stats)])

    fig, ax = plt.subplots(figsize=(8, 4.8), dpi=160)
    fig.patch.set_facecolor(c["surface"])
    ax.set_facecolor(c["surface"])

    ceiling = CEILING.get(instance)
    if ceiling:
        # A genuine threshold, so dashing carries meaning here rather than noise.
        ax.axhline(ceiling, color=c["muted"], lw=1, ls=(0, (5, 4)), zorder=1)
        ax.annotate(f"{instance} sustained, {ceiling:g} Gbps",
                    (0.995, ceiling), xycoords=ax.get_yaxis_transform(),
                    textcoords="offset points", xytext=(0, 5), ha="right",
                    fontsize=8, color=c["muted"])

    for i, (name, g) in enumerate(groups):
        col = c["series"][i % len(c["series"])]
        ax.fill_between(g["axis_value"], g["lo"], g["hi"], color=col,
                        alpha=0.18, lw=0, zorder=2)
        ax.plot(g["axis_value"], g["mean"], color=col, lw=2, marker="o",
                ms=6, mec=c["surface"], mew=1.5, label=name, zorder=3)

    bad = df[~df["valid"]]
    if len(bad):
        ax.scatter(bad["axis_value"], bad["gbps"], marker="x", s=55,
                   color=c["bad"], lw=1.8, zorder=4)
        ax.scatter([], [], marker="x", s=55, color=c["bad"], lw=1.8,
                   label="invalid run (excluded)")

    # Direct-label the peak only; the axis and the table carry the rest.
    best = stats.loc[stats["mean"].idxmax()]
    ax.annotate(f"{best['mean']:.1f} Gbps",
                (best["axis_value"], best["mean"]),
                textcoords="offset points", xytext=(0, 11), ha="center",
                fontsize=9, color=c["text"], fontweight="medium")

    ax.set_xscale("log", base=2)
    xs = sorted(stats["axis_value"].unique())
    ax.set_xticks(xs)
    ax.set_xticklabels([tick(axis, v) for v in xs], fontsize=9)
    ax.minorticks_off()
    ax.margins(x=0.06)          # room for the end labels
    # Always anchored at zero. Throughput is a magnitude, and cropping the
    # baseline turns a 1.7% spread into a dramatic-looking slope.
    ax.set_ylim(0, max(ceiling or 0, stats["hi"].max()) * 1.12)

    ax.set_xlabel(LABELS.get(axis, axis), fontsize=9.5, color=c["text"])
    ax.set_ylabel("Throughput (Gbps)", fontsize=9.5, color=c["text"])
    reps = int(stats["n"].max())
    fixed = ", ".join(f"{k}={tick(k, df[k].iloc[0])}"
                      for k in LABELS if k in df and k != axis)
    ax.set_title(title or f"smoltcp-s3 GET throughput vs {axis}",
                 fontsize=12, color=c["text"], loc="left", pad=16)
    ax.text(0, 1.02, f"{instance} · {fixed} · mean of {reps} runs, band is min–max",
            transform=ax.transAxes, fontsize=8.5, color=c["muted"])

    ax.grid(axis="y", color=c["grid"], lw=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(c["axis"])
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=c["muted"], labelsize=9, length=0)
    if series_col or len(bad):
        ax.legend(frameon=False, fontsize=9, labelcolor=c["text"])

    fig.tight_layout()
    fig.savefig(out, facecolor=c["surface"])
    plt.close(fig)

    md = out.with_suffix(".md")
    head = ([series_col] if series_col else []) + [axis, "mean Gbps", "min",
                                                   "max", "runs"]
    rows = [([str(r[series_col])] if series_col else [])
            + [tick(axis, r["axis_value"]), f"{r['mean']:.3f}",
               f"{r['lo']:.3f}", f"{r['hi']:.3f}", str(int(r["n"]))]
            for _, r in stats.iterrows()]
    md.write_text(f"# {axis} sweep — {instance}\n\n{fixed}\n\n"
                  f"{markdown(rows, head)}\n")
    return md


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path,
                    help="path to a sweep CSV, or a bench name whose "
                         "results/<name>/sweep.csv is used")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--series", default=None,
                    help="column to split into one line per value")
    ap.add_argument("--title", default=None)
    a = ap.parse_args()

    csv = a.csv if a.csv.is_file() else (
        Path(__file__).resolve().parents[2] / "results" / a.csv.name / "sweep.csv")
    if not csv.is_file():
        raise SystemExit(f"no such sweep CSV: {a.csv}")
    a.csv = csv
    df = pd.read_csv(csv)
    if df.empty:
        raise SystemExit(f"{csv} has no rows")
    mode = "dark" if a.dark else "light"
    out = a.out or a.csv.with_name(
        f"{a.csv.stem}{'-dark' if a.dark else ''}.png")
    md = plot(df, out, mode, a.series, a.title)
    valid = int(df["valid"].sum())
    print(f"wrote {out} and {md} ({valid}/{len(df)} runs valid)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
