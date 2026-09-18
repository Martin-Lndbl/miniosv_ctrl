#!/usr/bin/env python3
"""Where a query's wall time goes, from the miniOSv arm's own request stats.

    scripts/bench/breakdown.py results/tpch/miniosv-sf10-query4.csv --threads 64 \\
        --linux results/tpch/linux-sf10-query4-parity.csv

Wall time splits into the span with at least one S3 request outstanding
(net_active_ms) and DuckDB running with none. The outstanding span is
apportioned by a request's average life: S3's first-byte latency, the bytes
on the wire, and what remains -- queueing, wake-up, parsing -- which is the
guest's share. A Linux CSV with DuckDB's HTTP log (httplog=1) adds that
stack's per-request p50 as the footer's reference.
"""
import argparse
from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

PARTS = [("S3 first byte", "#c0504d"), ("wire transfer", "#e8a33d"),
         ("guest stack", "#2e7d32"), ("DuckDB, no request outstanding", "#4472c4")]


def split(g: pd.DataFrame) -> list[float]:
    m = g.median(numeric_only=True)
    per_call = m["net_ms"] / m["net_calls"]
    s3 = m["ttfb_us_avg"] / 1000 / per_call
    wire = m["xfer_us_avg"] / 1000 / per_call
    guest = max(0.0, 1 - s3 - wire)
    active = m["net_active_ms"]
    return [active * s3, active * wire, active * guest, max(0.0, m["query_ms"] - active)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path, nargs="+")
    ap.add_argument("--threads", type=int, default=64)
    ap.add_argument("--linux", type=Path, help="Linux CSV with http_ms_p50, for the footer")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()

    fig, axes = plt.subplots(1, len(a.csv), figsize=(4.5 * len(a.csv) + 1.5, 4.4), sharey=True, squeeze=False)
    for ax, csv in zip(axes[0], a.csv):
        df = pd.read_csv(csv)
        df = df[(df["threads"] == a.threads) & (df.get("valid", True) != False)]
        queries = sorted(df["query"].unique())
        rows = {q: split(df[df["query"] == q]) for q in queries}
        bottom = [0.0] * len(queries)
        for i, (label, color) in enumerate(PARTS):
            vals = [rows[q][i] for q in queries]
            ax.bar([f"Q{q:02d}" for q in queries], vals, bottom=bottom, color=color, label=label, width=0.6)
            bottom = [b + v for b, v in zip(bottom, vals)]
        for x, q in enumerate(queries):
            s3, wire, guest, _ = rows[q]
            ax.text(x, bottom[x], f"guest {100 * guest / sum(rows[q]):.1f}%", ha="center", va="bottom", fontsize=8)
        ax.set_title(f"{csv.stem}  ({a.threads} threads, medians)", fontsize=9)
        ax.set_ylabel("Query wall time (ms)")
        ax.grid(axis="y", alpha=0.3)
    axes[0][0].legend(fontsize=8, loc="upper left")
    foot = ("The span with a request outstanding, split by the average request's life:\n"
            "S3 first byte, bytes on the wire, and the rest (queue, wake-up, parse) as the guest's.")
    if a.linux:
        lx = pd.read_csv(a.linux)
        if "http_ms_p50" in lx and lx["http_ms_p50"].notna().any():
            foot += f"\nLinux DuckDB's own HTTP log on the same bucket: p50 {lx['http_ms_p50'].median():.0f} ms per request."
    fig.text(0.01, 0.01, foot, fontsize=7, va="bottom")
    fig.tight_layout(rect=(0, 0.1, 1, 1))
    out = a.out or a.csv[0].with_name(a.csv[0].stem + "-breakdown.png")
    fig.savefig(out, dpi=150)
    print(out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
