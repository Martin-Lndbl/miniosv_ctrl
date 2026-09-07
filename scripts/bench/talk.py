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

# Blue and orange mean miniOSv and Linux, in every figure of the evaluation
# section that has an OS axis. That only works if nothing else claims them:
# pmc-cost is miniOSv-only and its bars separate machines, not systems, so
# reusing slot 0 and 1 there would have taught the audience a colour code on
# one slide and contradicted it on the next.
#
# Violet/teal/slate, chosen to sit outside both reserved hue families -- no
# brown, which reads as a dark orange. Pitched at roughly the tint of the blue
# and orange they sit beside: a first attempt at #762a83/#1b7837/#4d4d4d
# separated cleanly but read as much heavier than the rest of the deck, which
# is light TUM blue on white. Hue does the separating and hatch plus value
# labels back it up, so the colours can afford to be this light.
MACHINE_PALETTE = ["#a17fd5", "#4fb8a8", "#8c96a0"]

# What a series is called on the figure, where it differs from its pooling key.
# "Linux (no throttle)" is the key -- it must stay distinct so captures under
# different kernel settings never average together -- but with the governor off
# both systems deliver the same rate, so the qualifier no longer describes
# anything the plot shows and only invited a question it does not answer. The
# condition is recorded in sampling.md.
LEGEND_LABEL = {"Linux (no throttle)": "Linux"}

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
    ax.set_title("PerfEvent overhead with 2 counters")
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
    # linuxnt is Linux with perf's rate throttling disabled -- a separate
    # series, since stock is what a user actually gets. This is the pooling
    # key, not the legend text: the two must stay distinct here or captures
    # taken under different kernel settings would average together. See
    # LEGEND_LABEL for what the figure calls it.
    if rest.startswith("linuxnt-"):
        return "Linux (no throttle)", rest[len("linuxnt-"):]
    if rest.startswith("linux-"):
        return "Linux", rest[len("linux-"):]
    return "miniOSv", rest


def clock_of(log: Path) -> float:
    """Core clock in MHz from a capture's console log, NaN if absent.

    Every pmc-* banner prints cpu_mhz, so this is read back from the boot that
    produced the numbers rather than assumed from the instance type -- the
    whole point is that two boots of the same type do not share a clock.
    """
    if not log.exists():
        return float("nan")
    m = re.search(r"cpu_mhz=([0-9.]+)", log.read_text(errors="ignore"))
    return float(m.group(1)) if m else float("nan")


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
        # Overhead against *requested* frequency is not comparable whenever the
        # two systems deliver different amounts: the same x position is then two
        # different amounts of work, and whichever delivered less looks cheaper
        # for having declined part of the job. Turning the kernel throttle off
        # removes the largest source of that gap but not all of it -- a sampled
        # second is stretched by the overhead itself, so neither system delivers
        # exactly what was asked.
        #
        # Cost per delivered sample removes that. In one sampled second the
        # added time is ovh/(1+ovh), shared among samples_per_s samples, so
        #   ns/sample = 1e9 * (ovh/(1+ovh)) / sps
        # Computed per row and then pooled, never from pooled medians.
        f = d["overhead_pct"] / 100.0
        d["ns_per_sample"] = 1e9 * (f / (1.0 + f)) / d["samples_per_s"]
        # The same cost with the boot's core clock divided out. Not plotted --
        # the figure is wall-clock, which is what a user experiences -- but
        # recorded, because nanoseconds carry whichever host EC2 handed out:
        # c7i.large boots here drew 3198 to 3722 MHz, a 16% spread, and the
        # miniOSv boots averaged slower than the Linux ones. Cycles put the
        # unsampled workload at 255.9 against 256.4 on the two systems, which
        # is the control that says the harness measures the same thing on both
        # sides; in nanoseconds those same rows differ by 2.5%.
        d["mhz"] = clock_of(csv.with_name(
            csv.name.replace("-sample.csv", ".log")))
        d["cycles_per_sample"] = d["ns_per_sample"] * d["mhz"] / 1e3
        # Only the Linux side reports this, and only since the throttle
        # instrumentation; absent means "not measured", which is not zero.
        if "throttles" not in d.columns:
            d["throttles"] = float("nan")
        d["boot"] = csv.name
        boots.setdefault(sample_id(csv), []).append(d)

    # Nothing to draw is a normal state, not a crash: the results directory is
    # empty for the whole of a re-run, and plotting the other two figures
    # should not depend on this one having data.
    if not boots:
        print("  no usable pmc-sample captures; skipping the sampling figure")
        return pd.DataFrame()

    # One panel, not two. The delivered-rate panel existed to show that Linux
    # declines part of the job at high frequencies -- but that shortfall was
    # perf_cpu_time_max_percent, and with the throttle off Linux tracks miniOSv,
    # so the panel became two curves lying on 100% and said nothing the cost
    # panel does not. delivered_pct is still computed, still gates `dead` reps
    # and still reaches the markdown table; it is the *plot* that dropped it,
    # because a validity check the reader has to be told is uninformative is
    # better checked than drawn.
    fig, ax_cost = plt.subplots(1, 1, figsize=(6.4, 4.0), dpi=200)

    # Colour is the OS and dash is the architecture, so the eye groups by what
    # is being compared -- miniOSv against Linux on the same silicon -- rather
    # than by machine. With four series the default colour cycle alone would
    # make every line look like a separate result.
    #
    # No-throttle takes the Linux slot when stock Linux is not in the figure,
    # so this keeps the two-colour OS pairing the counting figure uses rather
    # than introducing a third hue for what is still just "Linux". Both present
    # and they separate.
    stock_present = any(os_name == "Linux" for os_name, _ in boots)
    colour = {"miniOSv": PALETTE[0], "Linux": PALETTE[1],
              "Linux (no throttle)": PALETTE[2] if stock_present else PALETTE[1]}

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
                   cycles=("cycles_per_sample", "median"),
                   mhz=("mhz", "median"),
                   delivered=("delivered_pct", "median"),
                   dlo=("delivered_pct", lambda v: v.quantile(0.25)),
                   dhi=("delivered_pct", lambda v: v.quantile(0.75)),
                   throttles=("throttles", "median"),
                   boots=("boot", "nunique"), reps=("overhead_pct", "size"))
              .reset_index().sort_values("freq_hz"))
        # NaN, not zero: the cost at these frequencies is unmeasured, not
        # free. matplotlib leaves a gap rather than drawing a line to zero.
        #
        # `cycles` is masked with them, not after them. It is the same quantity
        # in another unit, so it is unmeasured wherever they are -- left
        # unmasked it reported -25703 cycles per sample at 99Hz, a negative
        # cost, which is what dividing noise by a clock produces.
        too_small = df["ovh_pct"] < COST_FLOOR_PCT
        df.loc[too_small, ["overhead", "olo", "ohi", "cycles"]] = float("nan")
        rows.append(df.assign(system=f"{os_name} {inst}"))

        col = colour.get(os_name, PALETTE[2])
        style = "--" if is_arm(inst) else "-"
        marker = "s" if is_arm(inst) else "o"
        arch = "aarch64" if is_arm(inst) else "x86-64"
        # ns -> us; the medians are stored in nanoseconds per delivered sample.
        sc = 1e3
        ax_cost.plot(df["freq_hz"], df["overhead"] / sc, style, color=col,
                     label=f"{LEGEND_LABEL.get(os_name, os_name)} · {inst} "
                           f"({arch})")
        ax_cost.fill_between(df["freq_hz"], df["olo"] / sc, df["ohi"] / sc,
                             color=col, alpha=0.12)
        # Hollow marks a point thin enough that its median is not worth the
        # same trust as the rest of the line.
        thin = df["reps"] < MIN_REPS
        ax_cost.plot(df["freq_hz"][~thin], df["overhead"][~thin] / sc, marker,
                     ms=5, color=col)
        ax_cost.plot(df["freq_hz"][thin], df["overhead"][thin] / sc, marker,
                     ms=5, mfc="white", mew=1.5, color=col)

    ax_cost.axvline(PERF_DEFAULT, color="grey", lw=1, ls=":")
    ax_cost.set_xscale("log")
    ax_cost.set_xlabel("Sampling frequency (Hz)")
    ax_cost.grid(True, alpha=0.3)
    ax_cost.set_axisbelow(True)
    # Nudged inside the axes rather than sat on the frame: at 0.97 with no y
    # offset the text rode the top border once the figure became a single
    # panel and gained height.
    ax_cost.annotate("perf default", (PERF_DEFAULT, 0.97),
                     xycoords=("data", "axes fraction"),
                     textcoords="offset points", xytext=(-5, -5), ha="right",
                     va="top", fontsize=8, color="grey")

    ax_cost.set_ylabel("Added time per delivered sample (µs)")
    ax_cost.set_ylim(0, None)
    # Starts at the first frequency whose overhead clears the noise floor.
    ax_cost.set_xlim(left=700)
    # Lower right, not upper: cost per sample is flat-to-rising, so the curves
    # live in the upper band and an upper-left legend sat on top of the c7i
    # line. Linux is the more expensive system, so it lands higher still and
    # leaves this corner clear.
    ax_cost.legend(loc="lower right")

    # A throttled rep is still worth knowing about even though the panel that
    # showed it is gone: with PERF_NO_THROTTLE set there should be none, and a
    # non-zero count means the sysctls did not take on that boot.
    thr = sum(int(d) for r in rows for d in r["throttles"] if d == d)
    if thr:
        print(f"  warning: {thr} PERF_RECORD_THROTTLE events in a "
              f"no-throttle run; check relax_perf() on those boots")

    fig.suptitle("Sampling overhead", y=0.99)
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
                    boots=raw["boot"].nunique(), vals=d.to_numpy())

    machines = [m for m in VENDOR if all((m, op) in stats for op, _ in PRIMS)]

    fig, ax = plt.subplots(figsize=(7.5, 4.0), dpi=200)
    width = 0.26
    x = np.arange(len(PRIMS))

    # Every repetition as a dot, over the median bar -- no error bar.
    #
    # An IQR whisker asserts one population with symmetric spread around a
    # centre, and Graviton's write is not that: it alternates between ~2.25us
    # and ~4.2us, roughly 2x apart, on a timescale of seconds. The whisker drew
    # a bar at 4.1 reaching down to 2.3 and implied the truth was somewhere in
    # between, which is the one value the operation never takes. The dots show
    # two clusters because there are two clusters.
    #
    # It costs nothing on the stable bars: read and stop hold to under 1% and
    # x86 write to under 1% within a boot, so their dots collapse into a line
    # and say "no spread" as clearly as a whisker would have.
    rng = np.random.default_rng(0)  # seeded: the jitter must not move between
    for i, machine in enumerate(machines):  # regenerations of the same figure
        pos = x + (i - (len(machines) - 1) / 2) * width
        vals = [stats[(machine, op)]["med"] for op, _ in PRIMS]
        bars = ax.bar(pos, vals, width, label=VENDOR[machine],
                      color=MACHINE_PALETTE[i], hatch=HATCH[i],
                      edgecolor="white", linewidth=0.8)
        ax.bar_label(bars, fmt="%.1f", padding=2, fontsize=7.5)
        for xi, (op, _) in zip(pos, PRIMS):
            v = stats[(machine, op)]["vals"]
            ax.scatter(xi + rng.uniform(-width * 0.28, width * 0.28, len(v)),
                       v, s=5, c="#2b2b2b", alpha=0.5, linewidths=0,
                       zorder=3)

    ax.set_xticks(x, [label for _, label in PRIMS])
    ax.set_ylabel("Wall-clock time per call (µs)")
    ax.set_title("Low-level primitive cost")
    ax.margins(y=0.18)
    ax.grid(True, axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    # Legend only. The conditions it carried -- boots, median, IQR -- are in
    # primitives.md; every one of these numbers comes from an EC2 guest, so
    # saying so in the title spent a line on a constant.
    ax.legend(loc="upper right")
    fig.tight_layout()
    save(fig, out)
    plt.close(fig)

    # `vals` is the per-rep array the dots are drawn from; it belongs to the
    # figure, not to a table of one row per (machine, op).
    return pd.DataFrame([
        dict(machine=m, op=op,
             **{k: v for k, v in stats[(m, op)].items() if k != "vals"})
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

    # Named for what it now shows: the fidelity panel is gone from the figure,
    # though delivered_pct stays in the table below.
    sout = RESULTS / "pmc-sample" / "sampling.png"
    stable = sampling(sout)
    # Guarded: an empty results directory during a re-run should not stop the
    # other two figures being written.
    if not stable.empty:
        sout.with_suffix(".md").write_text(
            "# Sampling — cost vs frequency\n\n"
            "Median and IQR over all reps of all boots. Points with fewer than "
            f"{MIN_REPS} reps are drawn hollow. delivered_pct is reported here "
            "rather than plotted: with the kernel throttle off it sits at the "
            "wall-clock-stretch prediction for both systems.\n\n"
            "The figure is nanoseconds, which is what a user experiences. "
            "`cycles_per_sample` is the same cost with `mhz` divided out, and "
            "is the fairer miniOSv-vs-Linux number: c7i.large boots drew "
            "3198-3722 MHz here, so a nanosecond figure carries whichever host "
            "that boot happened to get. The unsampled workload is identical "
            "code on both systems and lands within 0.2% in cycles, which is "
            "the control for that claim.\n\n"
            + stable[["system", "freq_hz", "boots", "reps", "overhead",
                      "cycles", "mhz", "ovh_pct", "delivered"]]
            .rename(columns={"overhead": "ns_per_sample",
                             "cycles": "cycles_per_sample",
                             "ovh_pct": "overhead_pct"})
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
        "**Do not quote `med` for c7g.large `pmc_write`.** That measurement is "
        "bimodal: over 8 boots and 40 repetitions, 21 land in a tight cluster "
        "at 2.21-2.34us, 15 in another at 4.06-4.33, and 4 in between -- reps "
        "that straddled a switch, since the cost alternates on a timescale of "
        "seconds. With two clusters of near-equal mass the median reports "
        "whichever one held the majority that day: it read 4.1 over the first "
        "3 boots and 2.3 over all 8, and neither is a value the operation "
        "spends much time at. The figure plots every repetition as a dot for "
        "this reason. The effect is specific to writing a counter and to "
        "aarch64 -- `pmc_read` and `pmc_stop` on the same boots hold to under "
        "1%, and x86 `pmc_write` holds to under 1% within a boot.\n\n"
        + ptable.to_markdown(index=False, floatfmt=".2f") + "\n"
    )
    print(f"wrote {pout}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
