#!/usr/bin/env python3
"""The 200 Gbps HTTP figure: throughput against pinned cores.

    scripts/bench/plot-http-200g.py [out.png]

The two-ENI arm is deliberately absent: it answers a different question
(a second NIC costs about 10% and adds no bandwidth, measured at 32/40/64
workers) and its 64-worker point stretches the axis so far that the rest of
the figure is unreadable. Its numbers are in
results/s3/miniosv-http-200g-2nic.csv.

All arms share an x axis: the cores the run was given. "Provisioned", not
"pinned to an RSS queue" -- that equivalence only holds on the unikernel,
where a worker owns a queue and a core together. Linux is given the same
core budget (irqbalance off, channels set to N, IRQs and XPS on cores
0..N-1) but the kernel is free to schedule softirq work as it likes, so its
cores are not bound to queues the way mininet's are. It keeps every feature
it would normally have; the second Linux curve drops it to smoltcp's
constraints -- MTU 1500 and no GRO -- and is there to price those rather
than as a fair comparison.

The third curve is `nogro1500`, not `parity`. Both drop Linux to MTU 1500
with GRO off, but parity also sets quickack, an rmem ceiling and busy poll
-- settings meant to make Linux resemble smoltcp that in fact cost it more
than losing jumbo does. Measured at 8 cores:

    capped     MTU 9001  GRO on                136.9 Gbps
    nogro      MTU 9001  GRO off               129.5      GRO:      -5%
    nogro1500  MTU 1500  GRO off                60.3      jumbo:   -53%
    parity     MTU 1500  GRO off  + 3 sysctls   23.2      sysctls: -62%

So parity is not "Linux under smoltcp's constraints", it is Linux hobbled
by configuration we imposed, and nogro1500 is what that arm was meant to
be. With GRO off each IP datagram is one wire frame, and at MTU 9001 that
frame is 7115 bytes, so **S3 does send jumbo**.

**And so does mininet, as of 2026-09-29**: the constexpr 1536 mbuf data room
is gone, so `miniosv-http-200g-jumbo` is the line to read and the 1500 one is
kept for the delta. At 8 cores that is 63.6 -> 108.4 Gbps aggregate (+70%),
96.6 -> 132.5 Gbps of frames; by 16 the endpoint absorbs most of it (109.8 ->
115.9 aggregate, 153.9 -> 173.0 of frames) -- which also says the 153.9 once
read as a single-ENI ceiling was partly a packet-rate limit, not bandwidth.

The pooled arm is **a different shape and does not belong on this axis as a
peer**: blocks=256 at 32 MiB against the sweep's blocks=0 at 128 MiB, because
with one block per connection the pool has nothing to redistribute. Same 256
GiB, but four times the connections, so its aggregate carries four times the
handshake cost. It is drawn to show where the sweep would sit if ranges were
claimed rather than owned, and it should be read on drain, not on this y axis:
at 32 workers the spread between first and last worker went 56.5% -> 32.5% of
the run and the every-slot-busy window went 2.14 s -> 11.30 s, while WIRE
STEADY barely moved (173.7 -> 174.6). The pool converts drain into steady
state; it does not make the wire faster.

Two caveats on that line. S3 caps its own segments at 8228 bytes of payload
-- Linux advertises 8949 on a 9001 path MTU and receives no more -- so 8294
bytes a frame is the ceiling for both stacks, not a miniOSv shortfall. And the
guest resolves a front-end per worker while S3's front-ends disagree about
jumbo (52.95.169.76 served nothing above 1514, on either stack), so check
`over 1514` in the run log before trusting a point; these were 98.7% and 96.5%.

Plotted on AGGREGATE -- payload over the whole run -- because it is defined
identically on both arms and is physical. TRANSFER, which excludes setup,
over-corrects on Linux: it subtracts the slowest handshake as though nothing
transferred while connections were coming up, which produced 206 Gbps on a
200 Gbps wire. Both columns are in the CSVs.
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
    # The two static-partition arms moved to archive/pre-shared-pool when the
    # shared block pool landed; they are kept on the figure because they are
    # the only miniOSv MTU pair that exists, and the pool arm beside them is
    # what says the scheduler changed. See that directory's README.
    ("archive/pre-shared-pool/miniosv-http-200g-jumbo", "miniOSv", "#15aabf", "D", "-"),
    ("miniosv-http-200g-jumbo-pool", "miniOSv (shared pool)", "#9c36b5", "v", ":"),
    ("archive/pre-shared-pool/miniosv-http-200g", "miniOSv (MTU 1500)", "#0b7285", "o", "--"),
    ("linux-http-200g-capped", "Linux", "#c92a2a", "s", "-"),
    ("linux-http-200g-nogro1500", "Linux (MTU 1500)", "#e8590c", "^", "--"),
]


def load(name):
    p = RESULTS / f"{name}.csv"
    if not p.exists():
        return []
    rows = []

    def num(v, cast=float):
        """Best effort: a field the guest did not report, or reported oddly
        (cores_over_50pct came back as "0.0" once), must not take the whole
        row with it -- only workers and gbps are load-bearing."""
        try:
            return cast(float(v))
        except (TypeError, ValueError):
            return None

    for r in csv.DictReader(p.open()):
        if r.get("valid") != "True":
            continue
        w, g = num(r.get("workers"), int), num(r.get("gbps"))
        if w is None or g is None:
            continue
        rows.append({
            "workers": w,
            "gbps": g,
            "transfer": num(r.get("transfer_gbps")),
            "cpu_s": num(r.get("cpu_s")),
            "per_cpu": num(r.get("gbps_per_cpu_s")),
            "active": num(r.get("cpus_active"), int),
            "elapsed": num(r.get("elapsed_s")),
        })
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
    for name, label, colour, marker, style in ARMS:
        rows = [r for r in load(name) if r["gbps"]]
        if not rows:
            # Silence here once cost a figure two of its five curves: an arm
            # whose CSV had been moved simply vanished, and the PDF looked
            # complete. Say which one, on stderr.
            print(f"  no rows for {name!r} -- curve {label!r} omitted", file=sys.stderr)
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

    ax.axhline(200, color="#adb5bd", ls=":", lw=1.2)
    ax.annotate("200 Gbps wire", xy=(0.985, 200), xycoords=("axes fraction", "data"),
                xytext=(0, -6), textcoords="offset points",
                ha="right", va="top", fontsize=8.5, color="#868e96")

    ax.set_ylim(0, 200)
    ax.set_xlabel("provisioned cores")
    ax.set_ylabel("Gbps (payload, whole run)")
    ax.set_title("HTTP GETs from S3: throughput")
    ax.grid(alpha=0.25)
    # upper left is the only empty quadrant: miniOSv rises through the
    # lower left, both Linux curves sit across the top right, and the
    # no-GRO curve runs along the bottom.
    ax.legend(fontsize=9, loc="upper left", framealpha=0.9)


    fig.tight_layout()
    fig.savefig(out, dpi=150)
    print(f"{out}  ({drawn} curve(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
