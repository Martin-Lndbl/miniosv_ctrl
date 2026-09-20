#!/usr/bin/env python3
"""Where a query's wall time goes, on miniOSv and (with its HTTP log) on Linux.

    scripts/bench/breakdown.py results/tpch/miniosv-sf10-breakdown.csv \\
        --linux results/tpch/linux-sf10-breakdown.csv

Each bar is the arm's median run of the query, by wall time. Wall time splits into the span with at least one S3 request
outstanding and DuckDB running with none. The outstanding span is apportioned by a request's
average life. On miniOSv the arm measures that life itself: S3's first-byte
latency, the bytes on the wire, and what remains (queueing, wake-up, parsing)
is the stack's. Linux reports a request's life through DuckDB's HTTP log
(httplog=1, a second logged pass whose own wall time is the bar); S3's part of
it is taken from the miniOSv arm's measurement of the same query, so the
remainder is what Linux's stack and client add.
"""
import argparse
import textwrap
from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# The coloured bands are not stopwatch readings of the query. They are the
# span of wall time with at least one request outstanding, split in the
# proportions of an average request's life, as the miniOSv worker stamps it:
#   turnaround  from the worker picking the request up (reusing a socket or
#               dialing one, sending the head) until the response's HTTP
#               headers are parsed: mostly S3's own latency, plus the round
#               trips and, on a fresh socket, the TCP and TLS handshakes;
#   body        from the headers until the last body byte lands;
#   the rest    waiting for a free worker slot before pick-up, and the
#               wake-up of the DuckDB thread after.
# Each request is timed on its own and averaged over the query. Linux has
# none of this: DuckDB's HTTP log gives one elapsed time per request, so a
# Linux bar is one band for the request's whole life and one for the time
# with nothing in flight. What the two arms share is the per-request life
# printed on each bar; the split of Linux's is unknown.
PARTS = [("S3 turnaround (worker pick-up to response headers in)", "#c0504d"),
         ("body on the wire", "#e8a33d"),
         ("network stack + HTTP client", "#2e7d32"),
         ("Linux: whole request life (DuckDB HTTP log)", "#8064a2"),
         ("DuckDB, no request outstanding", "#4472c4")]
METHOD = ("Bars: the median run's wall time; the number on top is an average request's life. Coloured bands: "
          "the span with at least one request outstanding, split by how an average request's life divides "
          "(each request timed on its own by the miniOSv worker, averaged over the query). Linux's HTTP log "
          "gives one elapsed time per request and no split, so its span is one band.")


def median_run(g: pd.DataFrame, wall: str) -> pd.Series:
    return g.sort_values(wall).iloc[(len(g) - 1) // 2]


def miniosv(g: pd.DataFrame) -> tuple[list[float], float, float, float]:
    """Wall-time parts, plus the request's S3 first-byte, wire and total ms."""
    m = median_run(g, "query_ms")
    per_call = m["net_ms"] / m["net_calls"]
    s3, wire = m["ttfb_us_avg"] / 1000, m["xfer_us_avg"] / 1000
    stack = max(0.0, per_call - s3 - wire)
    active = m["net_active_ms"]
    parts = [active * s3 / per_call, active * wire / per_call, active * stack / per_call, 0.0,
             max(0.0, m["query_ms"] - active)]
    return parts, s3, wire, per_call


def linux(g: pd.DataFrame) -> tuple[list[float], float]:
    """Wall-time parts and the request's average life; the log has no split."""
    m = median_run(g, "http_profile_ms")
    per_req = m["http_ms_sum"] / m["http_n"]
    window = m["http_window_ms"]
    return [0.0, 0.0, 0.0, window, max(0.0, m["http_profile_ms"] - window)], per_req


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path, help="the miniOSv arm")
    ap.add_argument("--linux", type=Path, help="the Linux arm, run with httplog=1")
    ap.add_argument("--threads", type=int, help="keep one thread count when the CSV has several")
    ap.add_argument("--title", default=None)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()

    df = pd.read_csv(a.csv)
    lx = pd.read_csv(a.linux) if a.linux else None
    for d in (df, lx):
        if a.threads and d is not None and "threads" in d:
            d.drop(d[d["threads"] != a.threads].index, inplace=True)
    if lx is not None:
        lx = lx[lx["http_n"].notna()]
    bars = []  # (x, label, parts, note on top)
    for i, q in enumerate(sorted(df["query"].unique())):
        parts, s3, wire, per_call = miniosv(df[df["query"] == q])
        stack_pct = 100 * parts[2] / sum(parts)
        note = f"{per_call:.0f} ms/req, {stack_pct:.1f}% stack"
        if lx is None:
            bars.append((i, f"Q{q:02d}", parts, note))
            continue
        bars.append((i * 3, f"Q{q:02d}\nminiOSv", parts, note))
        lq = lx[lx["query"] == q]
        line = f"Q{q:02d}: a request lives {per_call:.1f} ms on miniOSv (turnaround {s3:.1f}, body {wire:.1f})"
        if len(lq):
            lparts, lper = linux(lq)
            bars.append((i * 3 + 1, f"Q{q:02d}\nLinux", lparts, f"{lper:.0f} ms/req"))
            line += f", {lper:.1f} ms on Linux (no split)"
        print(line)

    fig, ax = plt.subplots(figsize=(1.1 * len(bars) + 3, 4.8))
    xs = [b[0] for b in bars]
    bottom = [0.0] * len(bars)
    for k, (label, color) in enumerate(PARTS):
        vals = [b[2][k] for b in bars]
        ax.bar(xs, vals, bottom=bottom, color=color, label=label if any(vals) else None, width=0.8)
        bottom = [b + v for b, v in zip(bottom, vals)]
    for (x, _, _, note), top in zip(bars, bottom):
        ax.text(x, top, note, ha="center", va="bottom", fontsize=7)
    ax.set_xticks(xs, [b[1] for b in bars], fontsize=8)
    ax.set_ylabel("Query wall time (ms)")
    ax.set_title(a.title or f"{a.csv.stem}: where the wall time goes (median run)", fontsize=10)
    ax.grid(axis="y", alpha=0.3)
    width_in = fig.get_size_inches()[0]
    note = textwrap.fill(METHOD, int(width_in * 13))
    note_h = 0.022 * (note.count("\n") + 1)  # figure fraction per 7 pt line, roughly
    fig.text(0.5, 0.005, note, ha="center", va="bottom", fontsize=7, color="#555555")
    ncol = 5 if width_in >= 18 else 3 if width_in >= 11 else 1
    shown = sum(1 for k in range(len(PARTS)) if any(b[2][k] for b in bars))
    rows = -(-shown // ncol)
    fig.legend(fontsize=8, loc="lower center", ncol=ncol, frameon=False, bbox_to_anchor=(0.5, note_h + 0.015))
    fig.tight_layout(rect=(0, note_h + 0.03 + 0.035 * rows, 1, 1))
    out = a.out or a.csv.with_name(a.csv.stem + "-breakdown.png")
    fig.savefig(out, dpi=150)
    print(out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
