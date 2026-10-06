"""Shared guest-side helpers, prepended to every bench's instance.py.

An instance.py is shipped as user-data text and cannot import anything that is
not on the AMI, so the driver concatenates CONFIG, this file and the bench's
own script into one program. That is why these live here rather than in a
module: the alternative was a copy per bench, which is what this replaces.

It mattered: `cap_cores` had drifted into three versions, and the one in
linux-s3 took no core count and never confined the client at all, so an arm
labelled "2 cores" ran on every vCPU of the machine. One definition cannot
drift.

Each bench defines BIN_NAME before this runs (the driver puts CONFIG first,
and Python resolves globals at call time, so order below this line is free).
"""
import os
import re
import shutil          # noqa: F401  -- used by the benches' own fetch code
import subprocess
import sys
import time            # noqa: F401
import urllib.request  # noqa: F401

try:                    # injected by the driver ahead of this file
    CONFIG              # type: ignore[used-before-def]  # noqa: B018
except NameError:
    CONFIG = {}


def cfg(key, default=""):
    v = CONFIG.get(key, os.environ.get(key, default))
    return "" if v is None else str(v)


LOCAL = cfg("BENCH_LOCAL", "0") == "1"
WORK = cfg("BENCH_WORK", "/run")
RUN_ID = cfg("RUN_ID", "unknown")
BUCKET, REGION = cfg("AWS_BUCKET"), cfg("AWS_REGION")
HOST = "{}.s3.{}.amazonaws.com".format(BUCKET, REGION)
SCHEME = cfg("BENCH_SCHEME", "https") or "https"
ENDPOINT = "{}://{}".format(SCHEME, HOST)

_console = None
if not LOCAL:
    try:
        _console = open("/dev/console", "w")
    except OSError:
        pass
_logfile = (open(os.path.join(WORK, cfg("BENCH_LOG", "instance") + ".log"), "a")
            if os.path.isdir(WORK) else None)

# Counters worth printing a delta for; everything else in ethtool -S is noise.
KEEP = re.compile(
    r"^(Tcp:(ActiveOpens|InSegs|OutSegs|RetransSegs|InErrs|AttemptFails|EstabResets)"
    r"|Ip:(InReceives|InDiscards|InHdrErrors)|Udp:InErrors"
    r"|rx_packets|tx_packets|rx_bytes|tx_bytes|rx_drops|tx_drops|rx_overruns"
    r"|.*allowance_exceeded)$")


def say(text=""):
    for sink in (sys.stdout, _console, _logfile):
        if sink is not None:
            try:
                sink.write(text + "\n")
                sink.flush()
            except OSError:
                pass


def rule(title):
    say()
    say("=== {} ===".format(title))


def run(*cmd, **kw):
    """stdout, never raising: a missing tool costs a fingerprint line, not the run."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=kw.get("timeout", 60))
        return (r.stdout or "") + (r.stderr or "" if kw.get("stderr") else "")
    except (OSError, subprocess.SubprocessError) as e:
        return "({}: {})".format(cmd[0], e)


def read(path, default=""):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def write(path, value):
    try:
        with open(path, "w") as fh:
            fh.write(str(value))
        return True
    except OSError:
        return False


def default_route():
    for line in run("ip", "-o", "-4", "route", "show", "default").splitlines():
        f = line.split()
        if "dev" in f:
            return f[f.index("dev") + 1], (f[f.index("via") + 1] if "via" in f else "")
    return "eth0", ""


def imds(path):
    try:
        req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        token = urllib.request.urlopen(req, timeout=2).read().decode()
        req = urllib.request.Request("http://169.254.169.254/latest/meta-data/" + path,
                                     headers={"X-aws-ec2-metadata-token": token})
        return urllib.request.urlopen(req, timeout=2).read().decode()
    except Exception:
        return "?"


def channels(iface):
    out = run("ethtool", "-l", iface).splitlines()
    cur = [ln.split()[-1] for ln in out if "Combined" in ln]
    return "{} combined of {} max".format(cur[-1] if cur else "?", cur[0] if cur else "?")


def gro_off(iface):
    """Receive aggregation removed and nothing else: the MTU stays as it is, so
    one IP datagram is one wire frame and no arm is credited with another's
    header overhead."""
    mtu = read("/sys/class/net/{}/mtu".format(iface))
    say("nogro        : GRO off, mtu {} left alone".format(mtu))
    run("ethtool", "-K", iface, "gro", "off")
    run("ethtool", "-K", iface, "lro", "off")
    gro = [l for l in run("ethtool", "-k", iface).splitlines()
           if l.startswith("generic-receive-offload")]
    say("             : {}, mtu {}".format(gro[0] if gro else "gro unknown", mtu))


def cap_cores(iface, n):
    """irqbalance off, one channel per core of the budget, IRQs and XPS on those
    cores. The client must additionally be confined (taskset, or its own
    sched_setaffinity) or only the softirq side is capped."""
    say("cap          : {} cores, {} channels, IRQs and XPS on 0-{}".format(n, n, n - 1))
    run("systemctl", "stop", "irqbalance")
    run("ethtool", "-L", iface, "combined", str(n))
    pinned = 0
    for line in read("/proc/interrupts").splitlines():
        if iface in line and ":" in line:
            irq = line.split(":", 1)[0].strip()
            if irq.isdigit() and write("/proc/irq/{}/smp_affinity_list".format(irq), pinned % n):
                pinned += 1
    xps, qdir = 0, "/sys/class/net/{}/queues".format(iface)
    for q in sorted(os.listdir(qdir) if os.path.isdir(qdir) else []):
        if q.startswith("tx-") and write("{}/{}/xps_cpus".format(qdir, q), format(1 << (xps % n), "x")):
            xps += 1
    say("             : {} IRQs pinned, XPS on {} tx queues, {}".format(pinned, xps, channels(iface)))


def percpu():
    """busy and total jiffies per cpu. A client's own getrusage sees neither
    io_uring's io-wq workers nor the softirq receive path; this does."""
    out = {}
    for line in read("/proc/stat").splitlines():
        f = line.split()
        if not f or not f[0].startswith("cpu") or f[0] == "cpu":
            continue
        v = [int(x) for x in f[1:]]
        out[int(f[0][3:])] = (sum(v) - v[3] - (v[4] if len(v) > 4 else 0), sum(v))
    return out


def report_cpu_budget(before, after, n):
    """Cores busy inside the budget and outside it. `cores_out` is the one that
    matters: nothing proves confinement until the kernel's own accounting is
    read back. Spans the whole process, so `cores_in` is diluted by setup and is
    not comparable to a figure measured around the transfer alone."""
    rule("cpu budget")
    inside = outside = 0.0
    hot = []
    for cpu, (b1, t1) in sorted(after.items()):
        b0, t0 = before.get(cpu, (0, 0))
        if t1 - t0 <= 0:
            continue
        frac = (b1 - b0) / (t1 - t0)
        if n and cpu >= n:
            outside += frac
            if frac > 0.02:
                hot.append("cpu{}={:.2f}".format(cpu, frac))
        else:
            inside += frac
    say("budget       : {}".format("0-{}".format(n - 1) if n else "all cpus"))
    if hot:
        say("outside >2%  : " + ", ".join(hot[:12]))
    say("CPU BUDGET: cores_in={:.2f} cores_out={:.2f} budget={}".format(inside, outside, n))


def counters(iface):
    out, header = {}, {}
    for line in read("/proc/net/snmp").splitlines():
        proto, _, rest = line.partition(":")
        fields = rest.split()
        if proto not in header:
            header[proto] = fields
        else:
            out.update({"{}:{}".format(proto, k): int(v) for k, v in zip(header[proto], fields)})
    for line in run("ethtool", "-S", iface).splitlines():
        name, _, value = line.partition(":")
        if value.strip().isdigit():
            out[name.strip()] = int(value.strip())
    return out


def report_counters(before, after):
    rule("counters")
    for name in sorted(before):
        if name in after and KEEP.match(name) and after[name] - before[name]:
            say("{:<30} {:+14d}   ({} -> {})".format(name, after[name] - before[name],
                                                     before[name], after[name]))
    for name, value in sorted(after.items()):
        if name.endswith("allowance_exceeded") and value > 0:
            say("{:<30} {:>14d}   <-- EC2 SHAPED".format(name, value))
