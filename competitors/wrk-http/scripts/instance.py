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

import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

try:
    CONFIG  # type: ignore[used-before-def]  # noqa: B018
except NameError:
    CONFIG = {}


def cfg(key, default=""):
    v = CONFIG.get(key, os.environ.get(key, default))
    return "" if v is None else str(v)


LOCAL = cfg("BENCH_LOCAL", "0") == "1"
WORK = cfg("BENCH_WORK", "/run")
RUN_ID = cfg("RUN_ID", "unknown")
ENDPOINT = "https://{}.s3.{}.amazonaws.com".format(cfg("AWS_BUCKET"), cfg("AWS_REGION"))
BIN_PREFIX = cfg("BENCH_BIN_PREFIX", "bin/wrk-http")
TARGET = cfg("AWS_TARGET_IP")
THREADS = cfg("BENCH_THREADS", "8")
CONNS = cfg("BENCH_CONNS", "128")
DURATION = cfg("BENCH_DURATION", "30")
BLOCK = cfg("BENCH_BLOCK_SIZE", str(128 << 20))
SIZE = cfg("BENCH_OBJECT_SIZE", str(10 << 30))
CLOSE = cfg("BENCH_CLOSE", "1")
BIN = os.path.join(WORK, "wrk")
LUA = os.path.join(WORK, "range.lua")

_console = None
if not LOCAL:
    try:
        _console = open("/dev/console", "w")
    except OSError:
        pass


def say(text=""):
    for sink in (sys.stdout, _console):
        if sink is not None:
            try:
                sink.write(text + "\n")
                sink.flush()
            except OSError:
                pass


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def read(path, default=""):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def default_route():
    for line in run("ip", "-o", "-4", "route", "show", "default").splitlines():
        f = line.split()
        if "dev" in f:
            return f[f.index("dev") + 1]
    return ""


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
    iface = default_route()
    rc = 1
    try:
        say("=== wrk-http ===")
        say("run id       : " + RUN_ID)
        say("target       : http://{}/blob.bin".format(TARGET))
        say("shape        : {} threads x {} conns, {} s, {} byte blocks, close={}".format(
            THREADS, CONNS, DURATION, BLOCK, CLOSE))
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
        env = dict(os.environ, BENCH_BLOCK_SIZE=BLOCK, BENCH_OBJECT_SIZE=SIZE, BENCH_CLOSE=CLOSE)
        cmd = [BIN, "-t", THREADS, "-c", CONNS, "-d", DURATION + "s", "--latency", "--timeout", "30s",
               "-s", LUA, "http://{}/blob.bin".format(TARGET)]
        say("cmd          : " + " ".join(cmd))
        n0, c0, t0 = nic(iface), cpu(), time.time()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=int(DURATION) + 180)
        except subprocess.TimeoutExpired:
            say("INCOMPLETE: wrk did not finish")
            return 1
        elapsed = time.time() - t0
        n1, c1 = nic(iface), cpu()
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
