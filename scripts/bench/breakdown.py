#!/usr/bin/env python3
"""Where a query's wall time goes, on miniOSv and (with its HTTP log) on Linux.

    scripts/bench/breakdown.py results/tpch/miniosv-sf10-breakdown.csv \\
        --linux results/tpch/linux-sf10-breakdown.csv

Each bar is the arm's median run of the query, by wall time. Wall time splits into the span with at least one S3 request
outstanding and DuckDB running with none. The outstanding span is apportioned by a request's
average life. On miniOSv the arm measures that life itself: S3's first-byte
latency, the bytes on the wire, and what remains (queueing, wake-up, parsing)
is the stack's. Linux reports a request's life through DuckDB's HTTP log (httplog=1,
a second logged pass whose own wall time is the bar). With netphase=1 that pass
also runs under competitors/duckdb-linux/netphase, which stamps every socket
send and receive, so the life splits the same way: S3's turnaround, the body
on the wire, and what the client and stack add -- each measured on Linux
rather than borrowed from the miniOSv arm. Without it the Linux band is one
striped block, split unknown.
"""
import argparse
import textwrap
from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.legend_handler import HandlerTuple  # noqa: E402
from matplotlib.patches import Patch, Polygon, Rectangle  # noqa: E402

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
# Each request is timed on its own and averaged over the query. On Linux the
# netphase shim stamps the same three moments at the socket: the request's
# last send, the first byte back, the last byte back; DuckDB's HTTP log gives
# the request's whole life, and the client's rest is the difference. A Linux
# run without the shim has only the log, and its band is one striped block.
PARTS = [("S3 turnaround (request out to response headers in; dial + TLS handshake on a fresh socket)", "#c0504d"),
         ("body on the wire", "#e8a33d"),
         ("network stack + HTTP client", "#2e7d32"),
         ("Linux: whole request life (DuckDB HTTP log), split not measured", "stripes"),
         ("DuckDB, no request outstanding", "#4472c4")]
METHOD = ("Bars: the median run's clean (uninstrumented) wall time; the number on top is an average request's life. "
          "Linux's split comes from a second, logged pass and is rescaled onto the clean bar. Coloured bands: "
          "the span with at least one request outstanding, split by how an average request's life divides. "
          "miniOSv: each request timed by the worker. Linux: each request's send, first and last byte stamped at "
          "the socket by an LD_PRELOAD shim in the same pass as DuckDB's HTTP log, which gives the whole life. "
          "A Linux receive is stamped when the syscall returns, so its turnaround and body include the kernel's "
          "receive path and the thread's wake-up; the miniOSv worker stamps the frame as it arrives.")


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


def linux(g: pd.DataFrame) -> tuple[list[float], float, float | None, float | None]:
    """Wall-time parts, the request's average life, and its turnaround and
    body when the run had the netphase shim (else None, None: one striped band).

    The bar is the **clean** pass's wall time, and the logged pass supplies
    only proportions. Instrumenting costs the Linux arm real time -- +3.0%,
    +12.2% and +46.1% over its own clean pass on three sf=100 reps -- so a bar
    drawn at `http_profile_ms` is up to half again too tall, and that is the
    arm the figure is trying to be fair to.
    """
    m = median_run(g, "query_ms")
    per_req = m["http_ms_sum"] / m["http_n"]
    # The union of the requests' intervals, like miniOSv's net_active_ms; the
    # first-to-last span for runs that predate it. Both come out of the logged
    # pass, so rescale them onto the clean pass's clock before splitting.
    active = m["http_active_ms"] if pd.notna(m.get("http_active_ms")) else m["http_window_ms"]
    scale = m["query_ms"] / m["http_profile_ms"] if m.get("http_profile_ms") else 1.0
    active = min(active * scale, m["query_ms"])
    rest = max(0.0, m["query_ms"] - active)
    if pd.notna(m.get("np_ttfb_ms_avg")):
        # np_hs_ms_avg is not believable -- 348-411 ms for an intra-region TLS
        # handshake that is 1-2 RTT -- so it is not folded into the turnaround
        # the way it once was. The handshake therefore falls into the residual
        # (the stack band) rather than inflating S3's by a quarter. miniOSv's
        # turnaround does include its handshakes, so the two arms differ here;
        # at 98%+ connection reuse it is a small asymmetry, but it is one.
        s3, wire = m["np_ttfb_ms_avg"], m["np_body_ms_avg"]
        stack = max(0.0, per_req - s3 - wire)
        return [active * s3 / per_req, active * wire / per_req, active * stack / per_req, 0.0, rest], per_req, s3, wire
    return [0.0, 0.0, 0.0, active, rest], per_req, None, None


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
            lparts, lper, ls3, lwire = linux(lq)
            if ls3 is not None:
                lnote = f"{lper:.0f} ms/req, {100 * lparts[2] / sum(lparts):.1f}% stack"
                line += f", {lper:.1f} ms on Linux (turnaround {ls3:.1f}, body {lwire:.1f})"
            else:
                lnote = f"{lper:.0f} ms/req"
                line += f", {lper:.1f} ms on Linux (no split)"
            bars.append((i * 3 + 1, f"Q{q:02d}\nLinux", lparts, lnote))
        print(line)

    fig, ax = plt.subplots(figsize=(1.1 * len(bars) + 3, 4.8))
    xs = [b[0] for b in bars]
    bottom = [0.0] * len(bars)
    striped = []  # Linux request-life bands: (x, bottom, height)
    handles, labels = [], []
    for k, (label, color) in enumerate(PARTS):
        vals = [b[2][k] for b in bars]
        if color == "stripes":
            striped += [(x, b, v) for x, b, v in zip(xs, bottom, vals) if v > 0]
            if striped:
                handles.append(tuple(Patch(facecolor=c) for _, c in PARTS[:3]))
                labels.append(label)
        else:
            ax.bar(xs, vals, bottom=bottom, color=color, width=0.8)
            if any(vals):
                handles.append(Patch(facecolor=color))
                labels.append(label)
        bottom = [b + v for b, v in zip(bottom, vals)]
    # The three miniOSv phases as diagonal stripes: the Linux band is made of
    # the same things, in proportions the log cannot tell.
    ymax = max(bottom) * 1.08
    step, rise = ymax * 0.015, ymax * 0.06
    for x, b, h in striped:
        clip = Rectangle((x - 0.4, b), 0.8, h, facecolor="none", edgecolor="none")
        ax.add_patch(clip)
        i = int((b - rise) // step)
        while i * step < b + h:
            y = i * step
            poly = Polygon([(x - 0.4, y), (x + 0.4, y + rise), (x + 0.4, y + step + rise), (x - 0.4, y + step)],
                           closed=True, facecolor=PARTS[i % 3][1], edgecolor="none")
            ax.add_patch(poly)
            poly.set_clip_path(clip)  # after add_patch, which resets the clip to the axes
            i += 1
    for (x, _, _, note), top in zip(bars, bottom):
        ax.text(x, top, note, ha="center", va="bottom", fontsize=7)
    ax.set_ylim(0, ymax)
    ax.set_xticks(xs, [b[1] for b in bars], fontsize=8)
    ax.set_ylabel("Query wall time (ms)")
    ax.set_title(a.title or f"{a.csv.stem}: where the wall time goes (median run)", fontsize=10)
    ax.grid(axis="y", alpha=0.3)
    width_in = fig.get_size_inches()[0]
    note = textwrap.fill(METHOD, int(width_in * 13))
    note_h = 0.022 * (note.count("\n") + 1)  # figure fraction per 7 pt line, roughly
    fig.text(0.5, 0.005, note, ha="center", va="bottom", fontsize=7, color="#555555")
    ncol = 5 if width_in >= 18 else 3 if width_in >= 11 else 1
    shown = len(handles)
    rows = -(-shown // ncol)
    fig.legend(handles, labels, fontsize=8, loc="lower center", ncol=ncol, frameon=False,
               bbox_to_anchor=(0.5, note_h + 0.015), handler_map={tuple: HandlerTuple(ndivide=3, pad=0)})
    fig.tight_layout(rect=(0, note_h + 0.03 + 0.035 * rows, 1, 1))
    out = a.out or a.csv.with_name(a.csv.stem + "-breakdown.png")
    fig.savefig(out, dpi=150)
    print(out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
