#!/usr/bin/env python3
"""EC2 user-data for the wrk arm of the HTTP load comparison; runs as root.

fingerprint -> fetch wrk and range.lua -> counters -> wrk against the nginx
the driver launched -> counters -> wait to be reaped. Same contract as
competitors/linux-s3: results on the serial console, COMPLETE:/INCOMPLETE:
last, no SSH, stdlib only. The driver prepends a `CONFIG = {...}` literal;
run directly it falls back to the environment:

    BENCH_LOCAL=1 BENCH_WORK=/tmp/w AWS_BUCKET=... AWS_REGION=... AWS_TARGET_IP=... \\
    python3 scripts/instance.py
"""


BIN_PREFIX = cfg("BENCH_BIN_PREFIX", "bin/linux-http")
TARGET = cfg("AWS_TARGET_IP")
THREADS = cfg("BENCH_THREADS", "8")
CONNS = cfg("BENCH_CONNS", "128")
DURATION = cfg("BENCH_DURATION", "30")
BLOCK = cfg("BENCH_BLOCK_SIZE", str(128 << 20))
SIZE = cfg("BENCH_OBJECT_SIZE", str(10 << 30))
CLOSE = cfg("BENCH_CLOSE", "1")
# 0 keeps the machine as shipped; N confines the whole receive path -- wrk's
# threads and the NIC's queues, IRQs and XPS -- to cores 0..N-1, the budget
# miniOSv's N busy-polling workers get.
CPUS = int(cfg("BENCH_CPUS", "0") or 0)
# "stock" leaves AL2023 as shipped (mtu 9001, GRO on -- six times fewer frames
# per byte than miniOSv can do); "parity" removes what mininet has not, so the
# two clients see the same wire. competitors/linux-s3 draws the same
# distinction and this is its match_smoltcp, minus the parts that belong to
# that binary's socket options.
MODE = cfg("MODE", "stock")
BIN = os.path.join(WORK, "wrk")
LUA = os.path.join(WORK, "range.lua")


def match_mininet(iface):
    """The same wire mininet has: no jumbo frames, no receive aggregation, no
    delayed ACK, a fixed receive ceiling. Costs Linux throughput on purpose --
    minidpdk's mbuf data room is a constexpr 1536, so 1500 is all miniOSv can
    ever offer, and a per-core comparison at 9001 would compare frame sizes."""
    mtu = "/sys/class/net/{}/mtu".format(iface)
    say("parity       : mtu {} -> 1500, GRO off, quickack, rmem ceiling fixed".format(read(mtu)))
    write(mtu, "1500")
    run("ethtool", "-K", iface, "gro", "off")
    run("ethtool", "-K", iface, "lro", "off")
    route = run("ip", "route", "show", "default").strip()
    if route and "quickack" not in route:
        run("ip", "route", "replace", *route.split(), "quickack", "1")
    write("/proc/sys/net/core/rmem_max", 16777216)
    write("/proc/sys/net/ipv4/tcp_rmem", "4096 131072 16777216")
    gro = [l for l in run("ethtool", "-k", iface).splitlines() if l.startswith("generic-receive-offload")]
    say("             : mtu {} now, {}, route {}".format(
        read(mtu), gro[0] if gro else "gro unknown",
        "quickack" if "quickack" in run("ip", "route", "show", "default") else "no quickack"))


def fetch(name, dest, mode=0o644):
    url = ENDPOINT + "/" + BIN_PREFIX + "/" + name
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as fh:
            shutil.copyfileobj(r, fh)
    except Exception as e:
        say("FAIL: could not fetch {}: {}".format(url, e))
        return False
    os.chmod(dest, mode)
    say("fetched {} ({} bytes)".format(name, os.path.getsize(dest)))
    return True


def nic(iface):
    base = "/sys/class/net/{}/statistics/".format(iface)
    return int(read(base + "rx_bytes", "0")), int(read(base + "rx_packets", "0"))


def cpu():
    """(busy, total) jiffies over every cpu, from /proc/stat's first line."""
    f = read("/proc/stat").splitlines()[0].split()[1:]
    v = [int(x) for x in f]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v) - idle, sum(v)


UNITS = {"B": 1, "KB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30, "TB": 1 << 40}
TIME_US = {"us": 1, "ms": 1000, "s": 1000000, "m": 60000000}


def latency_ms(s):
    m = re.match(r"([\d.]+)(us|ms|s|m)$", s)
    return float(m.group(1)) * TIME_US[m.group(2)] / 1000 if m else None


def main():
    iface, _ = default_route()
    rc = 1
    try:
        say("=== linux-http ===")
        say("run id       : " + RUN_ID)
        say("target       : http://{}/blob.bin".format(TARGET))
        say("shape        : {} threads x {} conns, {} s, {} byte blocks, close={}".format(
            THREADS, CONNS, DURATION, BLOCK, CLOSE))
        say("mode         : {}, cpu budget {}".format(MODE, CPUS or "all"))
        say("instance     : {} {}".format(imds("instance-type"), imds("instance-id")))
        say("az           : " + imds("placement/availability-zone"))
        say("kernel       : " + os.uname().release)
        say("nproc        : {}".format(os.cpu_count()))
        say("interface    : {} mtu {}".format(iface, read("/sys/class/net/{}/mtu".format(iface))))
        os.makedirs(WORK, exist_ok=True)
        if not (fetch("wrk", BIN, 0o755) and fetch("range.lua", LUA)):
            say("INCOMPLETE: binaries unavailable")
            return 1

        say("=== bench ===")
        if MODE == "parity":
            match_mininet(iface)
        elif MODE == "nogro":
            gro_off(iface)
        elif MODE != "stock":
            say("WARNING: unknown MODE={} — running as stock".format(MODE))
        if CPUS:
            cap_cores(iface, CPUS)
        env = dict(os.environ, BENCH_BLOCK_SIZE=BLOCK, BENCH_OBJECT_SIZE=SIZE, BENCH_CLOSE=CLOSE)
        cmd = [BIN, "-t", THREADS, "-c", CONNS, "-d", DURATION + "s", "--latency", "--timeout", "30s",
               "-s", LUA, "http://{}/blob.bin".format(TARGET)]
        if CPUS:
            cmd = ["taskset", "-c", "0-{}".format(CPUS - 1)] + cmd
        say("cmd          : " + " ".join(cmd))
        n0, c0, t0, pc0 = nic(iface), cpu(), time.time(), percpu()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=int(DURATION) + 180)
        except subprocess.TimeoutExpired:
            say("INCOMPLETE: wrk did not finish")
            report_cpu_budget(pc0, percpu(), CPUS)
            return 1
        elapsed = time.time() - t0
        n1, c1 = nic(iface), cpu()
        report_cpu_budget(pc0, percpu(), CPUS)
        for line in (p.stdout + p.stderr).splitlines():
            say(line)

        say("=== counters ===")
        rx_b, rx_p = n1[0] - n0[0], n1[1] - n0[1]
        say("RX: {} bytes, {} packets in {:.2f} s => {:.3f} Gbps on the wire".format(
            rx_b, rx_p, elapsed, rx_b * 8 / elapsed / 1e9))
        busy, total = c1[0] - c0[0], c1[1] - c0[1]
        frac = busy / total if total else 0.0
        say("CPU: busy={:.4f} cores={:.2f} of {}".format(frac, frac * os.cpu_count(), os.cpu_count()))
        # wrk's percentiles, in one line with units normalised to ms.
        lat = {}
        for pct in ("50", "75", "90", "99"):
            m = re.search(r"^\s+{}%\s+(\S+)".format(pct), p.stdout, re.M)
            if m and latency_ms(m.group(1)) is not None:
                lat["p" + pct] = latency_ms(m.group(1))
        m = re.search(r"^\s+Latency\s+(\S+)\s+(\S+)\s+(\S+)", p.stdout, re.M)
        if m:
            lat["avg"], lat["stdev"], lat["max"] = (latency_ms(x) for x in m.groups())
        say("LATENCY: " + " ".join("{}_ms={:.3f}".format(k, v) for k, v in lat.items() if v is not None))

        # wrk prints the run's length in its own unit: "1.02m" for a minute.
        m = re.search(r"(\d+) requests in ([\d.]+(?:us|ms|s|m)), ([\d.]+)(\w+) read", p.stdout)
        if p.returncode == 0 and m:
            b = float(m.group(3)) * UNITS.get(m.group(4), 1)
            say("AGGREGATE: {} requests, {:.3f} Gbps".format(m.group(1), b * 8 / (latency_ms(m.group(2)) / 1000) / 1e9))
            say("COMPLETE: {} requests, {}{} read".format(m.group(1), m.group(3), m.group(4)))
            rc = 0
        else:
            say("INCOMPLETE: wrk exit {}".format(p.returncode))
        return rc
    finally:
        if LOCAL:
            say("rc={} — BENCH_LOCAL=1, staying up".format(rc))
        else:
            # Powering off purges the console, the only copy of the results.
            say("rc={} — staying up for the console read".format(rc))
            subprocess.run(["sync"])
            subprocess.run(["shutdown", "-h", "+15"])


if __name__ == "__main__":
    sys.exit(main())
