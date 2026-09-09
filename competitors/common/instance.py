#!/usr/bin/env python3
"""EC2 user-data for a Linux baseline; runs as root under cloud-init.

fetch binary -> loosen perf_event_paranoid -> run -> print -> hold.

The driver prepends a `CONFIG = {...}` literal; run directly it falls back to
the environment, which is what a local dry-run uses:

    BENCH_BIN=./linux-sample python3 scripts/instance.py

No SSH, no keypairs: results come back on the serial console. The driver
terminates the instance once it has read them; a `shutdown -h +20` scheduled at
the top backstops a driver that dies first. Stdlib only — there is no pip on the
instance and no route to one.
"""

# No `from __future__ import annotations`: the driver injects a CONFIG literal
# ahead of this file, and a future import may only be preceded by the docstring.
import os
import subprocess
import time
import sys
import urllib.request

try:                    # injected by the driver ahead of this file
    CONFIG              # type: ignore[used-before-def]  # noqa: B018
except NameError:
    CONFIG = {}


def cfg(key, default=""):
    v = CONFIG.get(key, os.environ.get(key, default))
    return "" if v is None else str(v)


def say(msg):
    print("bench: {}".format(msg), flush=True)


KEY = cfg("BENCH_KEY", "linux-sample")
BIN = "/tmp/" + KEY
BUCKET = cfg("AWS_BUCKET")
REGION = cfg("AWS_REGION")
ENDPOINT = "https://{}.s3.{}.amazonaws.com".format(BUCKET, REGION)


def fetch_binary():
    local = cfg("BENCH_BIN")
    if local:
        say("using {} (BENCH_BIN set, skipping the S3 fetch)".format(local))
        return local
    # One key per architecture; the instance knows which it is.
    suffix = "-aarch64" if os.uname().machine == "aarch64" else ""
    url = ENDPOINT + "/bin/" + KEY + suffix
    say("fetching {}".format(url))
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(BIN, "wb") as fh:
            fh.write(r.read())
    except Exception as e:
        say("INCOMPLETE: binary unavailable: {}".format(e))
        return None
    os.chmod(BIN, 0o755)
    return BIN


def relax_perf():
    """Reported either way: it decides whether exclude_kernel=0 was honoured,
    and a run that silently sampled only userspace is a different measurement."""
    settings = [("/proc/sys/kernel/perf_event_paranoid", "-1"),
                ("/proc/sys/kernel/kptr_restrict", "0")]

    # Optional, off by default: the throttle is a real Linux design decision
    # and stock is what a user gets, so disabling it is a separate series
    # rather than a correction. With it off, both systems actually sample at
    # the rate on the axis, which is what makes cost-per-sample compare the
    # mechanism instead of the policy.
    #
    # Order matters. perf_event_max_sample_rate is recomputed whenever
    # perf_cpu_time_max_percent is written, so the percent has to go first or
    # the rate is clobbered right after being set.
    if cfg("PERF_NO_THROTTLE") == "1":
        settings += [("/proc/sys/kernel/perf_cpu_time_max_percent", "0"),
                     ("/proc/sys/kernel/perf_event_max_sample_rate", "1000000")]
        say("perf throttling: disabled")
    else:
        say("perf throttling: stock")

    for path, want in settings:
        try:
            with open(path, "w") as fh:
                fh.write(want)
        except Exception as e:
            say("could not set {}: {}".format(path, e))
        try:
            with open(path) as fh:
                say("{} = {}".format(path, fh.read().strip()))
        except Exception:
            pass


def main():
    # The comparison only holds against a miniOSv run on the same instance type.
    for path, label in (("/sys/devices/virtual/dmi/id/product_name", "product"),):
        try:
            with open(path) as fh:
                say("{}: {}".format(label, fh.read().strip()))
        except Exception:
            pass
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    say("cpu: " + line.split(":", 1)[1].strip())
                    break
    except Exception:
        pass

    relax_perf()

    binary = fetch_binary()
    if not binary:
        return 1

    say("running")
    r = subprocess.run([binary], stdout=sys.stdout, stderr=subprocess.STDOUT)
    say("exit {}".format(r.returncode))
    return r.returncode


if not cfg("BENCH_LOCAL"):
    # Backstop if the driver dies first. Scheduled up front so it covers a
    # crash in main() too.
    subprocess.run(["shutdown", "-h", "+20"], stderr=subprocess.DEVNULL)

rc = main()
sys.stdout.flush()

# Not shutting down: a terminated instance takes its serial console with it,
# and self-shutdown raced the driver's first poll into an empty log. The driver
# terminates us on seeing "pmc-sample: done".
say("holding for the driver to read the console")
sys.stdout.flush()
if not cfg("BENCH_LOCAL"):
    while True:
        time.sleep(60)
sys.exit(rc)
