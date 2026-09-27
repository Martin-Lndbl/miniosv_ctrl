#!/usr/bin/env python3
"""The HTTP load head-to-head: mininet (apps/bench/smoltcp-s3) against wrk on
Linux, each dialling its own nginx (competitors/nginx-static) in its own zone.

    scripts/bench/httpcmp.py results/http/miniosv-nginx.csv results/http/wrk-nginx.csv

One table and one figure per (instance, in-flight requests): throughput as the
client counted it and as frames on the wire, the cores the client kept busy,
and a request's life. The two arms measure differently, so the table says how:
wrk stamps every request; the mininet bench stamps handshakes and blocks, and
a request's mean life there is in-flight / (requests per second).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ARMS = ["miniOSv", "Linux"]
COLORS = {"miniOSv": "#2a78d6", "Linux": "#eb6834"}


def med(s: pd.Series) -> float:
    return float(s.median())


def rows(mini: pd.DataFrame, wrk: pd.DataFrame) -> pd.DataFrame:
    """One row per arm and point, valid reps only, medians and rep counts."""
    out = []
    for _, g in mini[mini["valid"]].groupby(["instance", "workers", "conns"]):
        inflight = int(g["workers"].iloc[0] * g["conns"].iloc[0])
        block = float(g["block"].iloc[0])
        reqs_s = g["gbps"] * 1e9 / 8 / block
        out.append(dict(arm="miniOSv", instance=g["instance"].iloc[0], inflight=inflight,
                        shape=f"{int(g['workers'].iloc[0])} workers x {int(g['conns'].iloc[0])} conns",
                        gbps=med(g["gbps"]), gbps_wire=med(g["gbps_wire"]), gbps_min=g["gbps"].min(), gbps_max=g["gbps"].max(),
                        req_s=med(reqs_s), cores=float(g["workers"].iloc[0]), cores_note="busy-polling workers",
                        lat_ms=med(inflight / reqs_s * 1000), lat_note="in-flight / req/s",
                        setup_p50_ms=med(g["setup_us_p50"]) / 1000, setup_p90_ms=med(g["setup_us_p90"]) / 1000,
                        reps=len(g), zone=",".join(sorted(g["zone"].unique()))))
    for _, g in wrk[wrk["valid"]].groupby(["instance", "threads", "conns"]):
        inflight = int(g["conns"].iloc[0])
        out.append(dict(arm="Linux", instance=g["instance"].iloc[0], inflight=inflight,
                        shape=f"{int(g['threads'].iloc[0])} threads x {inflight} conns",
                        gbps=med(g["gbps"]), gbps_wire=med(g["gbps_wire"]), gbps_min=g["gbps"].min(), gbps_max=g["gbps"].max(),
                        req_s=med(g["rps"]), cores=med(g["cpu_cores"]), cores_note="of /proc/stat, all 32 cpus",
                        lat_ms=med(g["lat_p50_ms"]), lat_note="wrk p50", lat_p99_ms=med(g["lat_p99_ms"]),
                        reps=len(g), zone=",".join(sorted(g["zone"].unique()))))
    return pd.DataFrame(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("miniosv", type=Path)
    ap.add_argument("linux", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--title", default=None)
    a = ap.parse_args()
    t = rows(pd.read_csv(a.miniosv), pd.read_csv(a.linux))
    if t.empty:
        raise SystemExit("no valid rows")
    pd.set_option("display.width", 200)
    print(t[["arm", "instance", "shape", "reps", "gbps", "gbps_min", "gbps_max", "gbps_wire", "req_s", "cores",
             "lat_ms", "setup_p50_ms", "setup_p90_ms", "lat_p99_ms"]].to_string(index=False, float_format=lambda v: f"{v:.2f}"))

    points = sorted(t["inflight"].unique())
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.6))
    panels = [("gbps", "Throughput, client-counted (Gbps)"), ("cores", "Cores busy"), ("lat_ms", "A request's life, median (ms)")]
    width = 0.38
    for ax, (col, label) in zip(axes, panels):
        for k, arm in enumerate(ARMS):
            sub = t[t["arm"] == arm].set_index("inflight").reindex(points)
            xs = [i + (k - 0.5) * width for i in range(len(points))]
            bars = ax.bar(xs, sub[col].fillna(0), width, color=COLORS[arm], label=arm)
            for b, v in zip(bars, sub[col]):
                if pd.notna(v):
                    ax.text(b.get_x() + b.get_width() / 2, b.get_height(), f"{v:.1f}", ha="center", va="bottom", fontsize=7)
        ax.set_xticks(range(len(points)), [f"{p} in flight" for p in points], fontsize=8)
        ax.set_title(label, fontsize=9)
        ax.grid(axis="y", alpha=0.3)
    axes[0].axhline(50, color="#898781", linestyle=":", linewidth=1)
    axes[0].text(0.02, 50, "50 Gbps line", fontsize=7, color="#898781", va="bottom", transform=axes[0].get_yaxis_transform())
    axes[1].legend(fontsize=8, frameon=False)
    inst = ", ".join(sorted(t["instance"].unique()))
    fig.suptitle(a.title or f"mininet vs wrk against nginx, {inst}, 128 MiB ranged GETs with Connection: close "
                 f"(medians of {int(t['reps'].min())}-{int(t['reps'].max())} reps)", fontsize=9)
    note = ("Cores: miniOSv's workers busy-poll, so its count is the worker count; Linux is /proc/stat busy time over 32 cpus. "
            "Life: wrk's p50 per request; on miniOSv in-flight / (requests per second), a mean.")
    fig.text(0.5, 0.005, note, ha="center", va="bottom", fontsize=7, color="#555555")
    fig.tight_layout(rect=(0, 0.06, 1, 0.95))
    out = a.out or a.miniosv.with_name("wrk-vs-mininet.png")
    fig.savefig(out, dpi=150)
    print(out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
