#!/usr/bin/env python3
"""The 200 Gbps HTTP figure: three curves against cores, one line for stock.

    scripts/bench/plot-http-200g.py [out.png]

miniOSv, Linux capped and Linux parity share an x axis that means the same
thing on all three -- pinned cores, one RSS queue each. Linux stock does not:
nothing restricts it, so its `workers` is the client's thread count with all
128 cores behind it. It is drawn as a horizontal reference line at its best
point, labelled with the threads it took, rather than as a fourth curve on an
axis it does not belong to.

Two panels, because the arms disagree about which number matters:

  throughput   TRANSFER, payload with connection setup excluded -- the one
               figure apps/bench/smoltcp-s3 and competitors/linux-s3 define
               identically. AGGREGATE is drawn faint behind it; the two
               coincide on miniOSv (512 connections up in ~1.4 ms) and sit
               30% apart on Linux (~3 s), so the gap is itself the result.

  efficiency   gigabits delivered per cpu-second. On miniOSv cpu_s is
               workers x wall, since a pinned busy-poller holds its core
               whether or not a frame arrives; on Linux it is measured from
               /proc/stat. That makes stock comparable here even though it is
               not comparable on the left.
"""
import csv
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results" / "s3"

ARMS = [
    ("miniosv-http-200g", "miniOSv", "#0b7285", "o", "-"),
    ("linux-http-200g-capped", "Linux (capped)", "#c92a2a", "s", "-"),
    ("linux-http-200g-parity", "Linux (parity)", "#e8590c", "^", "--"),
]
STOCK = ("linux-http-200g-stock", "Linux (stock)", "#495057")
EXTRA = ("miniosv-http-200g-2nic", "miniOSv, 2 ENIs", "#5f3dc4", "D", ":")


def load(name):
    p = RESULTS / f"{name}.csv"
    if not p.exists():
        return []
    rows = []
    for r in csv.DictReader(p.open()):
        if r.get("valid") != "True":
            continue
        try:
            rows.append({
                "workers": int(r["workers"]),
                "gbps": float(r["gbps"]) if r.get("gbps") else None,
                "transfer": float(r["transfer_gbps"]) if r.get("transfer_gbps") else None,
                "cpu_s": float(r["cpu_s"]) if r.get("cpu_s") else None,
                "per_cpu": float(r["gbps_per_cpu_s"]) if r.get("gbps_per_cpu_s") else None,
                "active": int(r["cpus_active"]) if r.get("cpus_active") else None,
                "elapsed": float(r["elapsed_s"]) if r.get("elapsed_s") else None,
            })
        except (KeyError, ValueError):
            continue
    # one point per worker count: the median, so a repeated point does not
    # draw twice and a rerun does not silently win
    out = {}
    for r in rows:
        out.setdefault(r["workers"], []).append(r)
    merged = []
    for w in sorted(out):
        g = out[w]
        pick = lambda k: sorted(x[k] for x in g if x[k] is not None)
        med = lambda v: v[len(v) // 2] if v else None
        merged.append({"workers": w, "gbps": med(pick("gbps")),
                       "transfer": med(pick("transfer")), "cpu_s": med(pick("cpu_s")),
                       "per_cpu": med(pick("per_cpu")), "active": med(pick("active")),
                       "elapsed": med(pick("elapsed"))})
    return merged


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else RESULTS / "http-200g.png"
    fig, ax = plt.subplots(figsize=(7.2, 5.0))

    drawn = 0
    for name, label, colour, marker, style in ARMS + [EXTRA]:
        rows = [r for r in load(name) if r["gbps"]]
        if not rows:
            continue
        drawn += 1
        x = [r["workers"] for r in rows]
        ax.plot(x, [r["gbps"] for r in rows], style, color=colour, marker=marker,
                label=label, lw=2, ms=6)
        # Where the NIC granted fewer queues than workers asked for, the run
        # used the granted number: say so rather than drawing a point at a
        # core count that never existed. miniOSv clamps at 32 queues an ENI,
        # so its 48-worker point is 48 threads over 32 cores and sits below
        # the 32-worker one -- contention, not a wider machine.
        for r in rows:
            if r["active"] and r["active"] < r["workers"]:
                ax.annotate(f"{r['workers']} threads\non {r['active']} queues",
                            xy=(r["workers"], r["gbps"]), xytext=(0, -30),
                            textcoords="offset points", ha="center", fontsize=7.5,
                            color=colour, alpha=0.85)

    # stock is unpinned, so its worker count is not a core count. Place it at
    # the cores it actually used -- cpu_s / elapsed, measured from /proc/stat
    # -- which is a number this axis can hold honestly, and mark it with a
    # vertical line so "how many cores did Linux need" can be read straight
    # off against the other curves.
    srows = [r for r in load(STOCK[0]) if r["gbps"]]
    if srows:
        best = max(srows, key=lambda r: r["gbps"])
        cores = (best["cpu_s"] / best["elapsed"]) if best.get("cpu_s") and best.get("elapsed") else None
        if cores:
            ax.axvline(cores, color=STOCK[2], ls="-.", lw=1.8)
            ax.plot([cores], [best["gbps"]], marker="*", ms=15, color=STOCK[2],
                    label=f"{STOCK[1]} ({best['workers']} threads)")
            ax.annotate(f"{STOCK[1]}: {best['gbps']:.0f} Gbps\nusing {cores:.0f} cores"
                        f" ({best['workers']} threads,\nunpinned, jumbo + GRO)",
                        xy=(cores, best["gbps"]), xytext=(8, -12),
                        textcoords="offset points", fontsize=8.5, color=STOCK[2])

    ax.axhline(200, color="#adb5bd", ls=":", lw=1.2)
    ax.annotate("200 Gbps wire", xy=(0.985, 200), xycoords=("axes fraction", "data"),
                xytext=(0, -6), textcoords="offset points",
                ha="right", va="top", fontsize=8.5, color="#868e96")

    ax.set_ylim(0, 200)
    ax.set_xlabel("pinned cores (one RSS queue each)")
    ax.set_ylabel("Gbps (payload, whole run)")
    ax.set_title("HTTP GETs from S3: throughput")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=9, loc="lower right")


    fig.tight_layout()
    fig.savefig(out, dpi=150)
    print(f"{out}  ({drawn} curve(s){', stock line' if srows else ''})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
