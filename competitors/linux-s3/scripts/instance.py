#!/usr/bin/env python3
"""EC2 user-data for the Tier-1 Linux baseline; runs as root under cloud-init.

fingerprint -> [tune] -> fetch binary -> run -> counter deltas -> wait to be reaped.

The driver prepends a `CONFIG = {...}` literal; run directly it falls back to
the environment, which is what the local dry-run uses:

    BENCH_LOCAL=1 BENCH_WORK=/tmp/w BENCH_BIN=./linux-s3 \
    AWS_BUCKET=... AWS_REGION=... AWS_TARGET_IP=... BENCH_WORKERS=8 ... \
    python3 scripts/instance.py

No SSH, no keypairs, results on the serial console. The driver terminates us
once it has read them; a timed shutdown backstops a forgotten run. Stdlib only
— no pip on the instance, and no route to one. See docs/tier1-linux-baseline.md §6.
"""

# No `from __future__ import annotations`: the driver injects a CONFIG
# literal ahead of this file, and a future import may only be preceded by
# the docstring — it would make the generated user-data a SyntaxError.
# json is used only when the driver injects CONFIG; keep the import local.
import os
import re
import shutil
import subprocess
import sys
import urllib.request

try:                    # injected by the driver ahead of this file
    CONFIG              # type: ignore[used-before-def]  # noqa: B018
except NameError:
    CONFIG = {}


def cfg(key, default=""):
    """Driver-injected value, else the environment, else a default."""
    v = CONFIG.get(key, os.environ.get(key, default))
    return "" if v is None else str(v)


LOCAL = cfg("BENCH_LOCAL", "0") == "1"
WORK = cfg("BENCH_WORK", "/run")
MODE = cfg("MODE", "stock")
RUN_ID = cfg("RUN_ID", "unknown")
BUCKET, REGION = cfg("AWS_BUCKET"), cfg("AWS_REGION")
ENDPOINT = "https://{}.s3.{}.amazonaws.com".format(BUCKET, REGION)
BIN = os.path.join(WORK, "linux-s3")

# Per-connection receive buffer, matching the smoltcp side's fixed 4 MiB rx ring.
RCVBUF = int(cfg("RCVBUF", str(4 * 1024 * 1024)))
# The unikernel pins one worker per RSS queue and the device gives it 8, so the
# parity arm gets that core budget rather than the machine's 32 (docs §7).
NQ = int(cfg("BENCH_WORKERS", "8") or 8)

# Allowlist: the full delta was ~60 lines of per-queue byte counts saying
# nothing the aggregate does not. These change how a run is *read*.
KEEP = re.compile(
    r"^(Tcp:(ActiveOpens|InSegs|OutSegs|RetransSegs|InErrs|AttemptFails|EstabResets)"
    r"|Ip:(InReceives|InDiscards|InHdrErrors)|Udp:InErrors"
    r"|rx_packets|tx_packets|rx_bytes|tx_bytes|rx_drops|tx_drops|rx_overruns"
    r"|.*allowance_exceeded)$")


# Everything funnels through say(), so the console sees the run in order,
# interleaved with the binary's own stdout.

_console = None
if not LOCAL:
    try:
        _console = open("/dev/console", "w")
    except OSError:
        pass
_logfile = open(os.path.join(WORK, "linux-s3.log"), "a") if os.path.isdir(WORK) else None


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
    """stdout, never raising: a missing tool costs a fingerprint line, not the
    run."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=kw.get("timeout", 60))
        return (r.stdout or "") + (r.stderr or "" if kw.get("stderr") else "")
    except (OSError, subprocess.SubprocessError) as e:
        return "({}: {})".format(cmd[0], e)


def write(path, value):
    """sysfs/procfs knob; True on success, so the caller reports what applied
    rather than what was attempted."""
    try:
        with open(path, "w") as fh:
            fh.write(str(value))
        return True
    except OSError:
        return False


def read(path, default=""):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def default_route():
    """(interface, gateway) from the main routing table."""
    for line in run("ip", "-o", "-4", "route", "show", "default").splitlines():
        f = line.split()
        if "dev" in f and "via" in f:
            return f[f.index("dev") + 1], f[f.index("via") + 1]
    return "", ""


def imds(path):
    """IMDSv2. Silent on failure: running off-instance is supported."""
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"})
        token = urllib.request.urlopen(req, timeout=3).read().decode()
        req = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/" + path,
            headers={"X-aws-ec2-metadata-token": token})
        return urllib.request.urlopen(req, timeout=3).read().decode()
    except Exception:
        return ""


def fingerprint(iface, gw):
    rule("run")
    say("run id       : " + RUN_ID)
    say("mode         : " + MODE)
    say("shape        : {} workers x {} conns x {} block".format(
        cfg("BENCH_WORKERS"), cfg("BENCH_CONNS_PER_WORKER"), cfg("BENCH_BLOCK_SIZE")))
    # The bench's scheme, not this script's: the binary fetch below stays HTTPS.
    say("object       : {}://{}.s3.{}.amazonaws.com/blob.bin ({})".format(
        cfg("BENCH_SCHEME", "https"), BUCKET, REGION, cfg("AWS_BUCKET_SIZE")))
    say("target ip    : " + cfg("AWS_TARGET_IP"))
    say("tls stub     : " + cfg("BENCH_TLS_STUB", "0"))
    say("scheme       : " + cfg("BENCH_SCHEME", "https"))  # http = no TLS at all

    rule("machine")
    say("instance     : {} {}".format(imds("instance-type"), imds("instance-id")))
    say("az           : " + imds("placement/availability-zone"))
    say("kernel       : " + os.uname().release)
    say("nproc        : {}".format(os.cpu_count()))
    say("interface    : {} (gw {})".format(iface, gw))

    rule("nic")
    # Only the fields that change how the stack behaves.
    for line in run("ethtool", "-i", iface).splitlines():
        if line.split(":")[0] in ("driver", "version", "firmware-version", "bus-info"):
            say(line)
    for line in run("ethtool", "-k", iface).splitlines():
        if line.split(":")[0] in (
                "rx-checksumming", "tx-checksumming", "generic-receive-offload",
                "large-receive-offload", "tcp-segmentation-offload",
                "generic-segmentation-offload"):
            say(line)
    say("mtu          : " + read("/sys/class/net/{}/mtu".format(iface)))
    say("channels     : " + channels(iface))

    rule("sysctls")
    for knob in ("net/core/rmem_max", "net/core/rmem_default", "net/ipv4/tcp_rmem",
                 "net/ipv4/tcp_window_scaling", "net/ipv4/tcp_moderate_rcvbuf",
                 "net/core/busy_poll", "net/core/busy_read", "net/ipv4/tcp_syn_retries"):
        say("{:28} = {}".format(knob.replace("/", "."),
                                read("/proc/sys/" + knob, "n/a")))

    rule("clock")
    # Skew surfaces as a rustls cert error, not a slow run.
    say("date         : " + run("date", "-u", "+%Y-%m-%dT%H:%M:%SZ").strip())
    tracking = run("chronyc", "tracking")
    for line in tracking.splitlines():
        if line.startswith(("Reference ID", "System time", "Leap status")):
            say(line)


def channels(iface):
    cur = mx = "?"
    section = None
    for line in run("ethtool", "-l", iface).splitlines():
        if line.startswith("Pre-set"):
            section = "max"
        elif line.startswith("Current"):
            section = "cur"
        elif line.startswith("Combined:"):
            v = line.split(":", 1)[1].strip()
            if section == "max":
                mx = v
            elif section == "cur":
                cur = v
    return "{} combined of {} max".format(cur, mx)


# The six knobs, each removing an advantage Linux has that smoltcp does not.
# Idempotent, and every step reports before -> after: a knob that silently
# failed to apply is a wrong measurement, not a slow one. The per-socket half is
# src/tune.rs. See docs/tier1-linux-baseline.md §7.

def step(title):
    say()
    say("-- " + title)


def show(label, value):
    say("   {:<12} {}".format(label, value))


def cap_cores(iface):
    """The unikernel gets 8 RSS queues, so letting Linux have all 32 cores
    is not a comparison. Used by both capped and parity."""
    # One queue per worker, connections pinned to the core handling it — the
    # closest analogue to one smoltcp worker owning one RSS queue.
    step("irqbalance off, {} channels, IRQs and XPS on cores 0-{}".format(NQ, NQ - 1))
    run("systemctl", "stop", "irqbalance")
    show("before", channels(iface))
    run("ethtool", "-L", iface, "combined", str(NQ))
    show("after", channels(iface))

    # ENA's IRQ naming is not guaranteed (docs ❓6): match loosely, report what
    # was found.
    pinned = 0
    for line in read("/proc/interrupts").splitlines():
        if iface in line and ":" in line:
            irq = line.split(":", 1)[0].strip()
            if irq.isdigit() and write(
                    "/proc/irq/{}/smp_affinity_list".format(irq), pinned % NQ):
                pinned += 1
    show("irqs", "pinned {} matching '{}'".format(pinned, iface))
    if pinned == 0:
        show("WARNING", "no ENA IRQs matched in /proc/interrupts (docs ❓6)")

    xps = 0
    qdir = "/sys/class/net/{}/queues".format(iface)
    for q in sorted(os.listdir(qdir) if os.path.isdir(qdir) else []):
        if q.startswith("tx-") and write(
                "{}/{}/xps_cpus".format(qdir, q), format(1 << (xps % NQ), "x")):
            xps += 1
    show("xps", "set on {} tx queues".format(xps))


def match_smoltcp(iface):
    """Remove what smoltcp does not have: jumbo frames, receive
    aggregation, delayed ACK and receive-window autotuning. Costs
    throughput on purpose."""
    mtu_path = "/sys/class/net/{}/mtu".format(iface)
    step("MTU 1500 (smoltcp's frame is 1514 = 1500 payload; ENA defaults to 9001)")
    show("before", read(mtu_path))
    write(mtu_path, "1500")
    show("after", read(mtu_path))

    def offloads():
        return " ".join(l for l in run("ethtool", "-k", iface).splitlines()
                        if l.startswith(("generic-receive-offload",
                                         "large-receive-offload")))

    step("GRO off (smoltcp has no receive aggregation)")
    show("before", offloads())
    run("ethtool", "-K", iface, "gro", "off")
    show("after", offloads())
    show("tso", "left as-is, receive-side workload")

    step("quickack on the default route (matches smoltcp's set_ack_delay(None))")
    before = run("ip", "route", "show", "default").strip()
    show("before", before)
    # Replace the route exactly as it stands, plus quickack. A partial spec
    # (`change default via GW dev IF`) does not match the dhcp route's metric,
    # so it failed into stderr, which was discarded and looked like success.
    if before:
        err = run("ip", "route", "replace", *before.split(), "quickack", "1",
                  stderr=True).strip()
        if err:
            show("warn", err)
    else:
        show("skip", "no default route; the socket option still applies")
    after = run("ip", "route", "show", "default").strip()
    show("after", after)
    if before and "quickack" not in after:
        show("warn", "route quickack did not apply; the socket option still does")

    # SO_RCVBUF in the binary disables autotuning; these raise the ceiling so
    # the request is not clamped.
    step("rmem ceiling (smoltcp's rx ring is a fixed 4 MiB, no autotuning)")
    show("before", "{} / {}".format(read("/proc/sys/net/core/rmem_max"),
                                    read("/proc/sys/net/ipv4/tcp_rmem")))
    write("/proc/sys/net/core/rmem_max", 16777216)
    write("/proc/sys/net/ipv4/tcp_rmem", "4096 131072 16777216")
    show("after", "{} / {}".format(read("/proc/sys/net/core/rmem_max"),
                                   read("/proc/sys/net/ipv4/tcp_rmem")))


    # SO_BUSY_POLL is per-socket in the binary and has been CAP_NET_ADMIN-gated
    # (docs ❓5); these sysctls are the fallback.
    step("busy poll (socket option is set by the binary; sysctls are the fallback)")
    show("before", "{} / {}".format(read("/proc/sys/net/core/busy_poll"),
                                    read("/proc/sys/net/core/busy_read")))
    write("/proc/sys/net/core/busy_poll", cfg("BUSY_POLL", "50"))
    write("/proc/sys/net/core/busy_read", cfg("BUSY_POLL", "50"))
    show("after", "{} / {}".format(read("/proc/sys/net/core/busy_poll"),
                                   read("/proc/sys/net/core/busy_read")))
    show("note", "24 busy-polling threads per core is expected to hurt — "
                 "a finding, not a bug")


# The analogue of the bench's `nic rx`/`nic tx` lines: a frame the device
# dropped never reaches the stack, and is indistinguishable from one never sent.

def counters(iface):
    out = {}
    # snmp alternates header/value lines per protocol:
    # "Ip: Forwarding DefaultTTL ..." then "Ip: 1 64 ...".
    header = {}
    for line in read("/proc/net/snmp").splitlines():
        proto, _, rest = line.partition(":")
        fields = rest.split()
        if proto not in header:
            header[proto] = fields
        else:
            for name, value in zip(header[proto], fields):
                out["{}:{}".format(proto, name)] = int(value)
    for line in run("ethtool", "-S", iface).splitlines():
        name, _, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if value.isdigit():
            out[name] = int(value)
    return out


def report_counters(before, after):
    rule("counters")
    for name in sorted(before):
        if name not in after or not KEEP.match(name):
            continue
        delta = after[name] - before[name]
        if delta:
            say("{:<30} {:+14d}   ({} -> {})".format(
                name, delta, before[name], after[name]))
    # Non-zero means EC2 shaped the run; tagged so the driver can find it.
    # Recorded, not disqualifying: the transfer is still complete and exact.
    for name, value in sorted(after.items()):
        if name.endswith("allowance_exceeded") and value > 0:
            say("{:<30} {:>14d}   <-- EC2 SHAPED".format(name, value))


# ---------------------------------------------------------------------------

def fetch_binary():
    rule("fetch")
    local_bin = cfg("BENCH_BIN")
    if local_bin:
        say("using {} (BENCH_BIN set, skipping the S3 fetch)".format(local_bin))
        shutil.copy(local_bin, BIN)
    else:
        # Unsigned, through the S3 gateway endpoint: no IAM role, no
        # credentials, no internet route.
        url = ENDPOINT + "/bin/linux-s3"
        try:
            with urllib.request.urlopen(url, timeout=120) as r, open(BIN, "wb") as fh:
                shutil.copyfileobj(r, fh)
        except Exception as e:
            say("FAIL: could not fetch {}: {}".format(url, e))
            say("INCOMPLETE: binary unavailable")
            return False
    os.chmod(BIN, 0o755)
    say("fetched {} bytes".format(os.path.getsize(BIN)))
    return True


def run_bench():
    """Streamed line by line, so a hung run still shows how far it got."""
    rule("bench")
    env = dict(os.environ)
    env.update({k: str(v) for k, v in CONFIG.items()})
    p = subprocess.Popen([BIN], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, env=env)
    assert p.stdout is not None
    for line in p.stdout:
        say(line.rstrip("\n"))
    p.wait()
    say("bench exit   : {}".format(p.returncode))
    return p.returncode


def main():
    if not os.path.isdir(WORK):
        os.makedirs(WORK, exist_ok=True)
    iface, gw = default_route()
    rc = 1
    try:
        fingerprint(iface, gw)

        rule("tune")
        # capped is the fair-resources arm: the unikernel's core budget, but
        # every Linux feature it would normally have. parity additionally
        # removes those features.
        if MODE not in ("stock", "capped", "parity"):
            say("WARNING: unknown MODE={} — running as stock".format(MODE))
        if MODE in ("capped", "parity"):
            cap_cores(iface)
        if MODE == "parity":
            match_smoltcp(iface)
        if MODE == "stock":
            say("MODE=stock — as-shipped AL2023 defaults, nothing changed")
        elif MODE == "capped":
            say()
            say("   kept         jumbo, GRO, delayed ACK, rmem autotuning")

        if not fetch_binary():
            return 1

        before = counters(iface)
        rc = run_bench()
        report_counters(before, counters(iface))
        return rc
    finally:
        if LOCAL:
            say("rc={} — BENCH_LOCAL=1, staying up".format(rc))
        else:
            # Powering off purges the console, which is the only copy of the
            # results. The driver reaps us; this timer catches a dead driver.
            say("rc={} — staying up for the console read".format(rc))
            subprocess.run(["sync"])
            subprocess.run(["shutdown", "-h", "+15"])


if __name__ == "__main__":
    sys.exit(main())
