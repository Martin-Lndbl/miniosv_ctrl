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


BIN = os.path.join(WORK, "anyblob-s3")


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
