#!/usr/bin/env python3
"""EC2 user-data for the AnyBlob competitor; runs as root under cloud-init.

fingerprint -> [pin the S3 name] -> fetch binary -> run -> counter deltas -> wait to be reaped.

The driver prepends a `CONFIG = {...}` literal; run directly it falls back to
the environment, which is what a local dry-run uses:

    BENCH_LOCAL=1 BENCH_WORK=/tmp/w BENCH_BIN=./anyblob-s3 \
    AWS_BUCKET=... AWS_REGION=... AWS_TARGET_IP=... BENCH_WORKERS=16 ... \
    python3 scripts/instance.py

Stock AL2023, nothing tuned: AnyBlob is measured as a user of the library
gets it. No SSH, no keypairs, results on the serial console; the driver
terminates us once it has read them, and a timed shutdown backstops a
forgotten run. Stdlib only.
"""

# No `from __future__ import annotations`: the driver injects a CONFIG
# literal ahead of this file.
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
    v = CONFIG.get(key, os.environ.get(key, default))
    return "" if v is None else str(v)


LOCAL = cfg("BENCH_LOCAL", "0") == "1"
WORK = cfg("BENCH_WORK", "/run")
RUN_ID = cfg("RUN_ID", "unknown")
BUCKET, REGION = cfg("AWS_BUCKET"), cfg("AWS_REGION")
HOST = "{}.s3.{}.amazonaws.com".format(BUCKET, REGION)
ENDPOINT = "https://" + HOST
BIN = os.path.join(WORK, "anyblob-s3")

KEEP = re.compile(
    r"^(Tcp:(ActiveOpens|InSegs|OutSegs|RetransSegs|InErrs|AttemptFails|EstabResets)"
    r"|Ip:(InReceives|InDiscards|InHdrErrors)|Udp:InErrors"
    r"|rx_packets|tx_packets|rx_bytes|tx_bytes|rx_drops|tx_drops|rx_overruns"
    r"|.*allowance_exceeded)$")

_console = None
if not LOCAL:
    try:
        _console = open("/dev/console", "w")
    except OSError:
        pass
_logfile = open(os.path.join(WORK, "anyblob-s3.log"), "a") if os.path.isdir(WORK) else None


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


def default_route():
    for line in run("ip", "-o", "-4", "route", "show", "default").splitlines():
        f = line.split()
        if "dev" in f and "via" in f:
            return f[f.index("dev") + 1], f[f.index("via") + 1]
    return "", ""


def imds(path):
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


def fingerprint(iface, gw):
    rule("run")
    say("run id       : " + RUN_ID)
    say("stack        : anyblob (io_uring, OpenSSL), stock AL2023")
    say("shape        : {} workers x {} conns x {} block, {} blocks/worker, chunk {}".format(
        cfg("BENCH_WORKERS"), cfg("BENCH_CONNS_PER_WORKER"), cfg("BENCH_BLOCK_SIZE"),
        cfg("BENCH_BLOCKS", "0"), cfg("BENCH_CHUNK", "65536")))
    say("object       : {} ({})".format(
        cfg("BENCH_URL") or "{}://{}/blob.bin".format(cfg("BENCH_SCHEME", "https"), HOST),
        cfg("AWS_BUCKET_SIZE")))
    say("cpus         : {}".format(cfg("BENCH_CPUS", "0") or "all"))
    say("target ip    : " + cfg("AWS_TARGET_IP"))
    say("scheme       : " + cfg("BENCH_SCHEME", "https"))

    rule("machine")
    say("instance     : {} {}".format(imds("instance-type"), imds("instance-id")))
    say("az           : " + imds("placement/availability-zone"))
    say("kernel       : " + os.uname().release)
    say("nproc        : {}".format(os.cpu_count()))
    say("interface    : {} (gw {})".format(iface, gw))
    say("io_uring     : kernel.io_uring_disabled = {}".format(
        read("/proc/sys/kernel/io_uring_disabled", "n/a (older kernel: enabled)")))

    rule("nic")
    for line in run("ethtool", "-i", iface).splitlines():
        if line.split(":")[0] in ("driver", "version", "firmware-version"):
            say(line)
    for line in run("ethtool", "-k", iface).splitlines():
        if line.split(":")[0] in ("generic-receive-offload", "large-receive-offload",
                                  "tcp-segmentation-offload"):
            say(line)
    say("mtu          : " + read("/sys/class/net/{}/mtu".format(iface)))
    say("channels     : " + channels(iface))

    rule("sysctls")
    for knob in ("net/core/rmem_max", "net/ipv4/tcp_rmem", "net/ipv4/tcp_window_scaling",
                 "net/ipv4/tcp_moderate_rcvbuf", "net/core/somaxconn", "fs/nr_open"):
        say("{:28} = {}".format(knob.replace("/", "."), read("/proc/sys/" + knob, "n/a")))
    say("{:28} = {}".format("ulimit -n (hard)", run("bash", "-c", "ulimit -Hn").strip()))

    rule("clock")
    say("date         : " + run("date", "-u", "+%Y-%m-%dT%H:%M:%SZ").strip())


def pin():
    """AnyBlob resolves the name per socket and, left alone, spreads over
    whatever DNS returns (its resolver is built to). BENCH_PIN=1 writes the
    given address into /etc/hosts, which getaddrinfo consults first, so Host
    and SNI stay the bucket's name and the certificate still verifies."""
    rule("resolver")
    ip = cfg("AWS_TARGET_IP")
    if cfg("BENCH_URL"):
        # An explicit URL already carries the address (the nginx arm dials a
        # private IP), so there is no name for /etc/hosts to intercept.
        say("pinned       : n/a, BENCH_URL is {}".format(cfg("BENCH_URL")))
        return
    if cfg("BENCH_PIN", "0") == "1" and ip:
        with open("/etc/hosts", "a") as fh:
            fh.write("\n{} {}\n".format(ip, HOST))
        say("pinned       : {} -> {} (/etc/hosts)".format(HOST, ip))
        say("resolves to  : " + run("getent", "ahostsv4", HOST).split("\n")[0])
    else:
        say("pinned       : no (AnyBlob resolves {} itself)".format(HOST))
        say("resolves to  : " + ", ".join(sorted({l.split()[0] for l in run("getent", "ahostsv4", HOST).splitlines() if l.strip()})))


def gro_off(iface):
    """Receive aggregation removed and nothing else -- the MTU stays at 9001.
    Verbatim from competitors/wrk-http: the two Linux arms of the nginx figure
    have to be shaped by the same code or the axis means different things."""
    mtu = read("/sys/class/net/{}/mtu".format(iface))
    say("nogro        : GRO off, mtu {} left alone".format(mtu))
    run("ethtool", "-K", iface, "gro", "off")
    run("ethtool", "-K", iface, "lro", "off")
    gro = [l for l in run("ethtool", "-k", iface).splitlines()
           if l.startswith("generic-receive-offload")]
    say("             : {}, mtu {}".format(gro[0] if gro else "gro unknown", mtu))


def cap_cores(iface, n):
    """irqbalance off, one channel per core of the budget, IRQs and XPS on
    those cores. The binary itself is confined by taskset in run_bench, so no
    softirq or daemon lands outside them."""
    say("cap          : {} cores, {} channels, IRQs and XPS on 0-{}".format(n, n, n - 1))
    run("systemctl", "stop", "irqbalance")
    run("ethtool", "-L", iface, "combined", str(n))
    pinned = 0
    for line in read("/proc/interrupts").splitlines():
        if iface in line and ":" in line:
            irq = line.split(":", 1)[0].strip()
            if irq.isdigit():
                try:
                    with open("/proc/irq/{}/smp_affinity_list".format(irq), "w") as fh:
                        fh.write(str(pinned % n))
                    pinned += 1
                except OSError:
                    pass
    xps, qdir = 0, "/sys/class/net/{}/queues".format(iface)
    for q in sorted(os.listdir(qdir) if os.path.isdir(qdir) else []):
        if q.startswith("tx-"):
            try:
                with open("{}/{}/xps_cpus".format(qdir, q), "w") as fh:
                    fh.write(format(1 << (xps % n), "x"))
                xps += 1
            except OSError:
                pass
    say("             : {} IRQs pinned, XPS on {} tx queues, channels now {}".format(
        pinned, xps, run("ethtool", "-l", iface).count("Combined") and
        [ln.split()[-1] for ln in run("ethtool", "-l", iface).splitlines() if "Combined" in ln][-1]))


def percpu():
    """busy and total jiffies per cpu, from /proc/stat's cpuN lines.

    `cores_avg` in the binary is getrusage(RUSAGE_SELF): process threads only.
    It sees neither io_uring's io-wq kernel workers nor the softirq receive
    path, both of which are real cost on a budget of N cores -- so it
    understates, and it cannot show whether anything ran outside the budget at
    all. This can."""
    out = {}
    for line in read("/proc/stat").splitlines():
        f = line.split()
        if not f or not f[0].startswith("cpu") or f[0] == "cpu":
            continue
        v = [int(x) for x in f[1:]]
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        out[int(f[0][3:])] = (sum(v) - idle, sum(v))
    return out


def report_cpu_budget(before, after, n):
    """How many cores were busy inside the budget, and how many outside it.

    A fraction per cpu (busy jiffies / total jiffies) needs no HZ and sums to
    cores. `cores_out` is the number that matters: taskset confines the
    daemons and cap_cores confines the IRQs, but nothing proves it until the
    kernel's own accounting is read back."""
    rule("cpu budget")
    inside = outside = 0.0
    hot = []
    for cpu, (b1, t1) in sorted(after.items()):
        b0, t0 = before.get(cpu, (0, 0))
        dt = t1 - t0
        if dt <= 0:
            continue
        frac = (b1 - b0) / dt
        if n and cpu >= n:
            outside += frac
            if frac > 0.02:
                hot.append("cpu{}={:.2f}".format(cpu, frac))
        else:
            inside += frac
    say("budget       : {}".format("0-{}".format(n - 1) if n else "all cpus"))
    say("cores busy   : {:.2f} inside, {:.2f} outside".format(inside, outside))
    if hot:
        say("outside >2%  : " + ", ".join(hot[:12]))
    say("CPU BUDGET: cores_in={:.2f} cores_out={:.2f} budget={}".format(inside, outside, n))


def counters(iface):
    out = {}
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
            say("{:<30} {:+14d}   ({} -> {})".format(name, delta, before[name], after[name]))
    for name, value in sorted(after.items()):
        if name.endswith("allowance_exceeded") and value > 0:
            say("{:<30} {:>14d}   <-- EC2 SHAPED".format(name, value))


def fetch_binary():
    rule("fetch")
    local_bin = cfg("BENCH_BIN")
    if local_bin:
        say("using {} (BENCH_BIN set, skipping the S3 fetch)".format(local_bin))
        shutil.copy(local_bin, BIN)
    else:
        # Unsigned through the gateway endpoint, before /etc/hosts is touched
        # so the fetch does not depend on the pinned front-end.
        url = ENDPOINT + "/bin/anyblob-s3"
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


def run_bench(iface):
    rule("bench")
    env = dict(os.environ)
    env.update({k: str(v) for k, v in CONFIG.items()})
    env["BENCH_IFACE"] = iface
    # Same trust store OpenSSL would use on AL2023; the binary is static and
    # carries none of its own.
    for ca in ("/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(ca):
            env.setdefault("SSL_CERT_FILE", ca)
            say("ca bundle    : " + ca)
            break
    cpus = int(cfg("BENCH_CPUS", "0") or 0)
    argv = ["taskset", "-c", "0-{}".format(cpus - 1), BIN] if cpus > 0 else [BIN]
    if cpus > 0:
        say("taskset      : 0-{}".format(cpus - 1))
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
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
        if not fetch_binary():
            return 1
        cpus = int(cfg("BENCH_CPUS", "0") or 0)
        if cfg("MODE") == "nogro":
            gro_off(iface)
        if cpus > 0:
            cap_cores(iface, cpus)
        pin()
        before, cpu0 = counters(iface), percpu()
        rc = run_bench(iface)
        report_cpu_budget(cpu0, percpu(), cpus)
        report_counters(before, counters(iface))
        return rc
    finally:
        if LOCAL:
            say("rc={} -- BENCH_LOCAL=1, staying up".format(rc))
        else:
            say("rc={} -- staying up for the console read".format(rc))
            subprocess.run(["sync"])
            subprocess.run(["shutdown", "-h", "+15"])


if __name__ == "__main__":
    sys.exit(main())
