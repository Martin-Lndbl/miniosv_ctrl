#!/usr/bin/env python3
"""The evaluation figures for the IDP talk.

    just plot-talk

Stock matplotlib: default style, default colour cycle, no theme. Everything
here is pooled across boots -- one boot cannot show between-boot spread, and
on these instances the core clock alone moves ~14% from boot to boot -- so
every value is a median over all reps of all boots and every error bar is the
IQR over that same population.

    counting.png  what a measured region costs, miniOSv against Linux
"""

from __future__ import annotations

import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results"

# Validated categorical palette, in fixed slot order -- never cycled.
# matplotlib's default tab10 puts blue/orange/green in the first three slots,
# and green against orange is the classic red-green confusion: a deuteranope
# reads the AMD and Graviton bars as the same colour. These three were checked
# with the palette validator (worst adjacent pair dE 9.2 deutan, 27.6 normal),
# so they separate under simulated CVD rather than only to normal vision.
#
# Colour is never the only channel. Bars carry a hatch and every bar its value;
# lines carry a dash pattern and a marker shape. Printed greyscale, or read by
# someone who cannot separate the hues, the figures still decode.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a"]
HATCH = ["", "//", "xx"]

# One workload size, stated on the figure. The cost is flat in region size --
# measured from 1ns to 74us, it never moves off its constant -- so a sweep
# spends a whole axis proving a line is horizontal. A bar at one size says the
# same thing and leaves room to say what the number is.
#
# 2000 is the smallest size whose baseline is comfortably clear of the
# run-to-run noise on every machine. Above 8k, miniOSv on Graviton drifts
# negative: the cost drops under the noise, which is a measurement limit and
# not a speed-up, and a bar cannot say that honestly.
KEYS = 2000

# Same generation on three vendors, which is the only reason putting them on
# one axis means anything. A machine with no capture for both systems is
# dropped rather than drawn half-height.
#
# Every machine runs 2 counters. Nitro grants a guest only 2 on Graviton --
# measured on c7g.large and c8g.large alike, so it is hypervisor policy, not a
# generation limit -- against 8 on c7i and 5 on c7a. An earlier version let
# each machine use what it was granted, which made Graviton measure half the
# work of the others while the figure put all three on one axis and invited
# the comparison anyway. Two is the largest count every target can serve
# without perf multiplexing, so it is the one that makes the bars mean the
# same thing everywhere.
MACHINES = [
    ("c7i.large", "Intel\n(c7i.large)"),
    ("c7a.large", "AMD\n(c7a.large)"),
    ("c7g.large", "Graviton 3\n(c7g.large)"),
]
# Same generation, three vendors -- the only reason one axis means anything.
VENDOR = {"c7i.large": "Intel", "c7a.large": "AMD", "c7g.large": "Graviton 3"}


def save(fig, out: Path) -> None:
    """Write both formats: PDF for the slide deck, PNG to look at.

    The deck embeds the PDF -- vector text stays sharp when a projector
    rescales it, and the axis labels do not go soft the way a rasterised
    figure does on a large screen.
    """
    fig.savefig(out)
    fig.savefig(out.with_suffix(".pdf"))


def load(paths) -> pd.DataFrame:
    """Every capture of a series is one boot; they are pooled, not deduplicated.

    One boot cannot show between-boot spread, and on these instances the core
    clock alone moves ~14% from boot to boot.
    """
    return pd.concat(
        [pd.read_csv(p).assign(boot=Path(p).name) for p in paths],
        ignore_index=True,
    )
SYSTEMS = ["miniOSv", "Linux"]


def run_id(csv: Path) -> tuple[str, str]:
    stem = csv.stem.removesuffix("-perfevent")
    _, _, rest = stem.partition("-")
    if rest.startswith("linux-"):
        return "Linux", rest[len("linux-"):]
    return "miniOSv", rest


def counting(out: Path) -> pd.DataFrame:
    # Every capture of a series is one boot; they are pooled, not deduplicated.
    boots: dict = {}
    for csv in sorted(RESULTS.glob("pmc-perfevent/*-perfevent.csv")):
        if csv.stat().st_size:
            boots.setdefault(run_id(csv), []).append(csv)

    stats = {}
    for key, paths in boots.items():
        raw = pd.concat(
            [pd.read_csv(p).assign(boot=p.name) for p in paths], ignore_index=True
        )
        d = raw[raw["keys"] == KEYS]["delta_ns"] / 1e3
        if d.empty:
            continue
        stats[key] = dict(
            med=d.median(), lo=d.quantile(0.25), hi=d.quantile(0.75),
            boots=raw["boot"].nunique(), reps=len(d),
        )

    # A machine only earns a slot once both systems have run on it: half a
    # pair is not a comparison, and an empty gap in the group reads as a zero.
    def complete(m: str) -> bool:
        return all((s, m) in stats for s in SYSTEMS)

    machines = [(m, lab) for m, lab in MACHINES if complete(m)]
    missing = [m for m, _ in MACHINES if not complete(m)]
    if missing:
        print(f"  no complete pair yet, skipping: {', '.join(missing)}")

    fig, ax = plt.subplots(figsize=(8.0, 4.2), dpi=200)
    width = 0.34
    x = np.arange(len(machines))

    for i, system in enumerate(SYSTEMS):
        vals = [stats[(system, m)]["med"] for m, _ in machines]
        err = np.array([
            [stats[(system, m)]["med"] - stats[(system, m)]["lo"] for m, _ in machines],
            [stats[(system, m)]["hi"] - stats[(system, m)]["med"] for m, _ in machines],
        ])
        bars = ax.bar(x + (i - 0.5) * width, vals, width, yerr=err, capsize=4,
                      label=system, color=PALETTE[i], hatch=HATCH[i],
                      edgecolor="white", linewidth=0.8)
        ax.bar_label(bars, fmt="%.0f µs", padding=3, fontsize=9)

    # The point of the figure, said as a number rather than left to the eye.
    for j, (m, _) in enumerate(machines):
        ratio = stats[("Linux", m)]["med"] / stats[("miniOSv", m)]["med"]
        top = stats[("Linux", m)]["hi"]
        ax.annotate(f"{ratio:.1f}× cheaper", (j, top), textcoords="offset points",
                    xytext=(0, 26), ha="center", fontsize=10, fontweight="bold")

    n = min(s["boots"] for s in stats.values())
    ax.set_xticks(x, [label for _, label in machines])
    ax.set_ylabel("Added time per measured region (µs)")
    ax.set_title("PerfEvent overhead (Virtualized)")
    ax.margins(y=0.22)
    ax.grid(True, axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    # Legend only, no caption line. The conditions it used to carry -- 4
    # counters, KEYS keys, n boots, median and IQR -- are in counting.md
    # beside the figure, so nothing is lost, and they belong in the spoken
    # part of a talk rather than in 8pt under a chart.
    #
    # Still below the axes: the tallest bar plus its value label and its ratio
    # annotation leave no corner big enough, and a legend overlapping a bar
    # makes the bar look shorter than it is.
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=2,
              frameon=False)
    fig.tight_layout()
    save(fig, out)
    plt.close(fig)

    return pd.DataFrame(
        [dict(system=s, machine=m, **stats[(s, m)])
         for s in SYSTEMS for m, _ in machines]
    )


# ---------------------------------------------------------------------------
# Sampling -- what it costs, and what you get for it
# ---------------------------------------------------------------------------
# Overhead alone says "we match Linux" and stops. The second panel is the part
# that matters: past ~10kHz the sampler stops keeping up, so the extra you pay
# buys fewer samples, not more. Cost is only half a sampling result.
PERF_DEFAULT = 4000
# Under this the sampler is not delivering what it was asked for, and an
# overhead measured there is not comparable with one measured above it.
DELIVERED_OK = 90.0
# Points thinner than this are drawn hollow. The 50kHz miniOSv point survived
# only 2 reps -- the 900s deadline truncates the sweep before the rest run --
# so it is on the figure but must not be read as the others are.
MIN_REPS = 10
# Below this overhead there is nothing to divide. At 99Hz the sampled run is
# 0.03-0.3% slower than the baseline, against a measured noise floor of
# 0.007%, so the per-sample cost derived from it is noise amplified by a large
# reciprocal -- it came out at -2.6us on Linux/x86 and 31us on miniOSv/x86,
# neither of which is a cost. Those points are dropped from the cost panel and
# kept in the delivery panel, where no division happens.
COST_FLOOR_PCT = 0.5


def sample_id(csv: Path) -> tuple[str, str]:
    stem = csv.stem.removesuffix("-sample")
    _, _, rest = stem.partition("-")
    if rest.startswith("linux-"):
        return "Linux", rest[len("linux-"):]
    return "miniOSv", rest


def sampling(out: Path) -> pd.DataFrame:
    boots: dict = {}
    for csv in sorted(RESULTS.glob("pmc-sample/*-sample.csv")):
        if not csv.stat().st_size:
            continue
        d = pd.read_csv(csv)
        # Captures from before the per-rep schema cannot be pooled with the
        # ones that have it.
        if not {"rep", "dead", "base_ns", "sampled_ns"} <= set(d.columns):
            continue
        # A rep that armed but never fired is broken, not cheap.
        d = d[(d["dead"] == 0) & (d["base_ns"] > 0)].copy()
        d["overhead_pct"] = 100 * (d["sampled_ns"] - d["base_ns"]) / d["base_ns"]
        # Overhead against *requested* frequency is not comparable once one
        # system throttles: at 50kHz Linux delivers ~16.5k/s and miniOSv
        # ~29k/s, so the same x position is two different amounts of work and
        # Linux looks cheaper largely for having declined the job.
        #
        # Cost per delivered sample removes that. In one sampled second the
        # added time is ovh/(1+ovh), shared among samples_per_s samples, so
        #   ns/sample = 1e9 * (ovh/(1+ovh)) / sps
        # Computed per row and then pooled, never from pooled medians.
        f = d["overhead_pct"] / 100.0
        d["ns_per_sample"] = 1e9 * (f / (1.0 + f)) / d["samples_per_s"]
        # Only the Linux side reports this, and only since the throttle
        # instrumentation; absent means "not measured", which is not zero.
        if "throttles" not in d.columns:
            d["throttles"] = float("nan")
        d["boot"] = csv.name
        boots.setdefault(sample_id(csv), []).append(d)

    # Not sharex: the cost panel has no measurable point at 99Hz, and a shared
    # axis forced it to render an empty decade that read as missing data.
    # Each panel now spans the range over which its own quantity exists.
    fig, (ax_cost, ax_fid) = plt.subplots(1, 2, figsize=(9.6, 4.0), dpi=200)

    # Colour is the OS and dash is the architecture, so the eye groups by what
    # is being compared -- miniOSv against Linux on the same silicon -- rather
    # than by machine. With four series the default colour cycle alone would
    # make every line look like a separate result.
    colour = {"miniOSv": PALETTE[0], "Linux": PALETTE[1]}

    def is_arm(inst: str) -> bool:
        return re.match(r"^[a-z]+\dg", inst) is not None

    rows = []
    for (os_name, inst), frames in sorted(boots.items()):
        raw = pd.concat(frames, ignore_index=True)
        df = (raw.groupby("freq_hz")
              .agg(overhead=("ns_per_sample", "median"),
                   olo=("ns_per_sample", lambda v: v.quantile(0.25)),
                   ohi=("ns_per_sample", lambda v: v.quantile(0.75)),
                   ovh_pct=("overhead_pct", "median"),
                   delivered=("delivered_pct", "median"),
                   dlo=("delivered_pct", lambda v: v.quantile(0.25)),
                   dhi=("delivered_pct", lambda v: v.quantile(0.75)),
                   throttles=("throttles", "median"),
                   boots=("boot", "nunique"), reps=("overhead_pct", "size"))
              .reset_index().sort_values("freq_hz"))
        # NaN, not zero: the cost at these frequencies is unmeasured, not
        # free. matplotlib leaves a gap rather than drawing a line to zero.
        too_small = df["ovh_pct"] < COST_FLOOR_PCT
        df.loc[too_small, ["overhead", "olo", "ohi"]] = float("nan")
        rows.append(df.assign(system=f"{os_name} {inst}"))

        col = colour.get(os_name, PALETTE[2])
        style = "--" if is_arm(inst) else "-"
        marker = "s" if is_arm(inst) else "o"
        arch = "aarch64" if is_arm(inst) else "x86-64"
        for ax, med, lo, hi in ((ax_cost, "overhead", "olo", "ohi"),
                                (ax_fid, "delivered", "dlo", "dhi")):
            ax.plot(df["freq_hz"], df[med] / (1e3 if med == "overhead" else 1),
                    style, color=col,
                    label=f"{os_name} · {inst} ({arch})")
            sc = 1e3 if med == "overhead" else 1
            ax.fill_between(df["freq_hz"], df[lo] / sc, df[hi] / sc,
                            color=col, alpha=0.12)
            # Hollow marks a point thin enough that its median is not worth the
            # same trust as the rest of the line.
            thin = df["reps"] < MIN_REPS
            ax.plot(df["freq_hz"][~thin], df[med][~thin] / sc, marker, ms=5,
                    color=col)
            ax.plot(df["freq_hz"][thin], df[med][thin] / sc, marker, ms=5,
                    mfc="white", mew=1.5, color=col)

    for ax in (ax_cost, ax_fid):
        ax.axvline(PERF_DEFAULT, color="grey", lw=1, ls=":")
        ax.set_xscale("log")
        ax.set_xlabel("Sampling frequency (Hz)")
        ax.grid(True, alpha=0.3)
        ax.set_axisbelow(True)
    ax_cost.annotate("perf default", (PERF_DEFAULT, 0.97),
                     xycoords=("data", "axes fraction"),
                     textcoords="offset points", xytext=(-5, 0), ha="right",
                     va="top", fontsize=8, color="grey")

    ax_cost.set_ylabel("Added time per delivered sample (µs)")
    ax_cost.set_title("Cost per sample")
    ax_cost.set_ylim(0, None)
    # Starts at the first frequency whose overhead clears the noise floor.
    ax_cost.set_xlim(left=700)

    # The shaded "below 90%" band and its caption are gone: the y axis already
    # says what fraction arrived, so the band restated the reading in prose
    # and cost more ink than the data.
    ax_fid.set_ylabel("Samples delivered (% of requested)")
    ax_fid.set_title("Delivered rate")
    ax_fid.set_ylim(0, 105)
    ax_fid.legend(loc="lower left")

    # Where Linux's own governor starts cutting the rate. Below this point the
    # whole shortfall is the wall-clock stretch that both systems share -- at
    # 10kHz, 16.5% overhead predicts 85.8% delivery and 86% arrives. At 50kHz
    # the kernel emits PERF_RECORD_THROTTLE and takes off half again, which is
    # perf_cpu_time_max_percent working as designed rather than a defect.
    # Anchored on the throttled point and set just above it, in the wedge
    # between the descending Linux curves and the bottom of the panel. At 45%
    # it ran along those curves; below the point it collided with the legend.
    thr = [(f, d, dl) for r in rows for f, d, dl in
           zip(r["freq_hz"], r["throttles"], r["delivered"])
           if d == d and d > 0]
    if thr:
        first = min(f for f, _, _ in thr)
        at = min(dl for f, _, dl in thr if f == first)
        ax_fid.annotate("PERF_RECORD_THROTTLE", (first, at),
                        textcoords="offset points", xytext=(-6, 24),
                        ha="right", fontsize=8, color=PALETTE[1],
                        arrowprops=dict(arrowstyle="->", color=PALETTE[1],
                                        lw=1))

    fig.suptitle("Sampling overhead and fidelity (Virtualized)", y=0.99)
    fig.tight_layout()
    save(fig, out)
    plt.close(fig)

    return pd.concat(rows, ignore_index=True)


# ---------------------------------------------------------------------------
# The primitives, one bar each -- the honest-limits figure
# ---------------------------------------------------------------------------
# The six-call interface is uniform to *call*, not uniform to *pay*. Graviton
# programs a counter in ~5.9us and reads one in ~0.8us; Intel does both in
# roughly 1.3-1.5us. Same code above the line, completely different cost shape
# underneath. loop_overhead is omitted: it is the sub-nanosecond floor the
# others sit on and it would be an invisible bar.
#
# Wall-clock, not cycles. Both come from the same loop -- one steady_clock
# read, N identical calls on one register, one read after, divide by N -- but
# the cycle figure is that time multiplied by a separately calibrated
# cpu_hz(), so it inherits any drift between the 50ms calibration and the
# measurement. Nanoseconds are what the loop actually observed, and they are
# also the unit the other two evaluation figures use.
#
# The cost: nanoseconds do not normalise for clock speed, so part of the gap
# between two machines is that they tick at different rates. cycles_per_op is
# still in primitives.md for anyone who wants the normalised view.
PRIMS = [
    ("pmc_start_with_conf", "start"),
    ("pmc_read", "read"),
    ("pmc_write", "write"),
    ("pmc_stop", "stop"),
]


def primitives(out: Path) -> pd.DataFrame:
    stats = {}
    for machine in VENDOR:
        paths = sorted(RESULTS.glob(f"pmc-cost/*-{machine}-prim.csv"))
        if not paths:
            continue
        raw = load(paths)
        for op, _ in PRIMS:
            d = raw[raw["op"] == op]["ns_per_op"] / 1e3
            if not d.empty:
                stats[(machine, op)] = dict(
                    med=d.median(), lo=d.quantile(0.25), hi=d.quantile(0.75),
                    boots=raw["boot"].nunique())

    machines = [m for m in VENDOR if all((m, op) in stats for op, _ in PRIMS)]

    fig, ax = plt.subplots(figsize=(7.5, 4.0), dpi=200)
    width = 0.26
    x = np.arange(len(PRIMS))

    for i, machine in enumerate(machines):
        vals = [stats[(machine, op)]["med"] for op, _ in PRIMS]
        err = np.array([
            [stats[(machine, op)]["med"] - stats[(machine, op)]["lo"]
             for op, _ in PRIMS],
            [stats[(machine, op)]["hi"] - stats[(machine, op)]["med"]
             for op, _ in PRIMS],
        ])
        bars = ax.bar(x + (i - (len(machines) - 1) / 2) * width, vals, width,
                      yerr=err, capsize=3, label=VENDOR[machine],
                      color=PALETTE[i], hatch=HATCH[i], edgecolor="white",
                      linewidth=0.8)
        ax.bar_label(bars, fmt="%.1f", padding=2, fontsize=7.5)

    ax.set_xticks(x, [label for _, label in PRIMS])
    ax.set_ylabel("Wall-clock time per call (µs)")
    ax.set_title("Low-level primitive cost (Virtualized)")
    ax.margins(y=0.18)
    ax.grid(True, axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    # Legend only. The conditions it carried -- boots, median, IQR -- are in
    # primitives.md, and "(Virtualized)" in the title now says the thing the
    # second line was there to say.
    ax.legend(loc="upper right")
    fig.tight_layout()
    save(fig, out)
    plt.close(fig)

    return pd.DataFrame([dict(machine=m, op=op, **stats[(m, op)])
                         for m in machines for op, _ in PRIMS])


def main() -> int:
    out = RESULTS / "pmc-perfevent" / "counting.png"
    table = counting(out)
    out.with_suffix(".md").write_text(
        f"# Counting cost, {KEYS} keys\n\n"
        "Added microseconds per measured region. Median and IQR over all reps "
        "of all boots.\n\n"
        "Two counters on every machine and both systems. Nitro grants a guest "
        "only 2 on Graviton (measured on c7g.large and c8g.large alike) "
        "against 8 on c7i and 5 on c7a, so 2 is the largest count every "
        "target serves without perf multiplexing -- which makes the bars "
        "comparable across machines, not only within one.\n\n"
        + table.to_markdown(index=False, floatfmt=".1f") + "\n"
    )
    print(f"wrote {out}")

    sout = RESULTS / "pmc-sample" / "fidelity.png"
    stable = sampling(sout)
    sout.with_suffix(".md").write_text(
        "# Sampling — cost and fidelity vs frequency\n\n"
        "Median and IQR over all reps of all boots. Points with fewer than "
        f"{MIN_REPS} reps are drawn hollow.\n\n"
        + stable[["system", "freq_hz", "boots", "reps", "overhead",
                  "ovh_pct", "delivered"]]
        .rename(columns={"overhead": "ns_per_sample", "ovh_pct": "overhead_pct"})
        .to_markdown(index=False, floatfmt=".2f") + "\n"
    )
    print(f"wrote {sout}")

    pout = RESULTS / "pmc-cost" / "primitives.png"
    ptable = primitives(pout)
    pout.with_suffix(".md").write_text(
        "# Cost of each primitive\n\n"
        "Wall-clock microseconds per call: one steady_clock read, N identical "
        "calls on one register, one read after, divided by N. Median and IQR "
        "over all reps of all boots. Under a hypervisor every counter access "
        "traps, so these are VM-exit costs, not instruction costs.\n\n"
        + ptable.to_markdown(index=False, floatfmt=".2f") + "\n"
    )
    print(f"wrote {pout}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
