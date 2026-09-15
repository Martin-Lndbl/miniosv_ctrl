#!/usr/bin/env python3
"""EC2 user-data for the DuckDB-on-Linux competitor; runs as root under
cloud-init. Real DuckDB, real sockets, real OpenSSL -- the baseline
apps/bench/duckdb-tpch is measured against.

fetch duckdb + httpfs -> create the 8 TPC-H views over the same S3 bucket ->
run each requested query, timed -> compare against tpch_answers() -> print ->
wait to be reaped.

The driver prepends a `CONFIG = {...}` literal; run directly it falls back to
the environment, which is what a local dry-run uses:

    BENCH_LOCAL=1 BENCH_WORK=/tmp/w AWS_BUCKET=... AWS_REGION=... \
    BENCH_SF=1 BENCH_QUERIES=6 python3 scripts/instance.py

No SSH, no keypairs: results come back on the serial console. The driver
terminates the instance once it has read them; a `shutdown -h +15` scheduled
at the end backstops a driver that dies first. Stdlib only -- no pip on the
instance, and no route to one.

Prints the same log-line shapes app/miniduckdb/miniosv/main.cc's tpch
executable does (Q06: ... ms, ... rows, match=...  /  TPCH SUMMARY: ...  /
COMPLETE|INCOMPLETE: ...), so scripts/bench/duckdb-linux/bench.py's regexes
and scripts/bench/duckdb-tpch/bench.py's land in directly comparable CSV
columns.
"""

# No `from __future__ import annotations`: the driver injects a CONFIG
# literal ahead of this file, and a future import may only be preceded by
# the docstring.
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

try:                    # injected by the driver ahead of this file
    CONFIG              # type: ignore[used-before-def]  # noqa: B018
except NameError:
    CONFIG = {}


def cfg(key, default=""):
    v = CONFIG.get(key, os.environ.get(key, default))
    return "" if v is None else str(v)


def say(text=""):
    print(text, flush=True)


LOCAL = cfg("BENCH_LOCAL", "0") == "1"
WORK = cfg("BENCH_WORK", "/run")
BUCKET, REGION = cfg("AWS_BUCKET"), cfg("AWS_REGION")
HOST = "{}.s3.{}.amazonaws.com".format(BUCKET, REGION)
ENDPOINT = "https://{}".format(HOST)

# Pin the bucket to one S3 front-end, the way a mininet image is built: it
# compiles in a single address and every connection dials it. DuckDB here
# resolves the name normally and may spread its connections over whatever
# DNS hands back, so without this the two sides are not asking the same
# question. Empty means "resolve normally".
PIN_IP = cfg("BENCH_PIN_IP")
if PIN_IP:
    with open("/etc/hosts", "a") as fh:
        fh.write("\n{} {}\n".format(PIN_IP, HOST))
    say("pinned {} -> {}".format(HOST, PIN_IP))
SF = cfg("BENCH_SF", "1")
QUERIES = [int(x) for x in cfg("BENCH_QUERIES", "6").split(",") if x.strip()]

# A second, separately timed pass with DuckDB's own HTTP request log on. The
# miniOSv arm counts its requests inside mininet; this counts them inside
# DuckDB, above whichever client is underneath, which is the only place the
# two stacks can be compared at the same layer. Off by default: it is a
# diagnostic, and the numbers the comparison plots come from the clean pass.
HTTP_LOG = cfg("BENCH_HTTP_LOG") == "1"

# "1" replaces the TPC-H-over-S3 run with the no-network CPU ladder that
# app/miniduckdb/miniosv/main.cc's `cpuprobe` executable runs, step for step
# and query for query. The HTTP-log comparison put the sf=10 gap in the work
# *between* reads rather than in the reads themselves, and none of that needs
# S3 to measure -- so this arm stops touching it.
CPU_PROBE = cfg("BENCH_CPU_PROBE") == "1"

# DuckDB's thread count, as `SET threads=N`. Empty leaves its own default of
# one per cpu, which is what every reading so far used. It exists because the
# miniOSv arm has had `--threads` all along, and a tuning knob offered to one
# side and not the other is not a comparison -- oversubscribing threads is
# worth something to a query that spends most of each thread's life waiting on
# S3, and both stacks are entitled to it.
THREADS = cfg("BENCH_THREADS")

# One expensive predicate per lineitem row, for the par_t1/par_tall pair whose
# ratio is the parallel speedup actually delivered. It has to be a table scan:
# range() parallelises to about 1.5 threads on either stack, so the range_scan
# and hash_agg steps measure single-core speed and cannot see this.
PROBE_PREDICATE = (
    "SELECT count(*) FROM lineitem WHERE "
    "ln(1+abs(sin(l_extendedprice))) + ln(1+abs(cos(l_quantity))) + "
    "ln(1+abs(sin(l_discount))) + ln(1+abs(cos(l_tax))) + "
    "sqrt(abs(l_extendedprice)) + sqrt(abs(l_quantity)) + "
    "ln(2+abs(sin(l_tax))) + sqrt(1+abs(l_discount)) > -1"
)

# Kept identical to probe_steps[] in main.cc -- same names, same order, same
# sizes. `pre` runs untimed ahead of the step, so the thread count a step is
# measured at is not itself in the measurement. Changing one list without the
# other silently compares two different ladders.
PROBE_STEPS = [
    ("dbgen", None, "CALL dbgen(sf=1)"),
    ("range_scan", None,
     "SELECT count(*) FROM range(1000000000) WHERE range % 7 = 0"),
    ("hash_agg", None,
     "SELECT count(*) FROM (SELECT range % 1000 AS k, sum(range) "
     "FROM range(250000000) GROUP BY k)"),
    ("par_t1", "SET threads=1", PROBE_PREDICATE),
    ("par_tall", "RESET threads", PROBE_PREDICATE),
    ("q01_local", None, "PRAGMA tpch(1)"),
    ("q06_local", None, "PRAGMA tpch(6)"),
]

# Parity with mininet's receive path. Left alone, Linux gets two things the
# miniOSv side cannot have: GRO, which hands the stack one big coalesced
# segment instead of each 1460-byte frame, and one RX/TX queue per vCPU, where
# mininet polls MININET_WORKERS of them. Neither is a defect -- they are why
# you would run Linux -- but with both in play the comparison is not measuring
# the same shape of work. Empty means "leave the NIC as the kernel set it up",
# which is the stock arm.
GRO = cfg("BENCH_GRO")            # "off" to disable GRO and LRO
QUEUES = cfg("BENCH_NIC_QUEUES")  # combined RX/TX queue count, "" to leave alone


def default_iface():
    """The interface the default route leaves by -- ENA is ens5 on most
    instance types and eth0 on some, so neither name can be assumed."""
    try:
        out = subprocess.run(["ip", "-o", "route", "get", "1.1.1.1"],
                             capture_output=True, text=True, timeout=10).stdout
        parts = out.split()
        return parts[parts.index("dev") + 1] if "dev" in parts else ""
    except Exception:
        return ""


def ethtool(args):
    r = subprocess.run(["ethtool"] + args, capture_output=True, text=True, timeout=30)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def tune_nic():
    """Apply the parity settings and report what the NIC actually ended up
    with, read back rather than assumed. The driver gates validity on this
    line, so a tune that silently failed cannot be mistaken for a parity run."""
    if not GRO and not QUEUES:
        return
    iface = default_iface()
    if not iface:
        say("nic: FAILED no default-route interface")
        return
    if shutil.which("ethtool") is None:
        say("nic: FAILED ethtool not installed (no route to a package mirror)")
        return

    if GRO == "off":
        for feature in ("gro", "lro"):
            rc, out = ethtool(["-K", iface, feature, "off"])
            # LRO is unsupported on ENA; refusing it is not a failure.
            if rc != 0 and feature == "gro":
                say("nic: FAILED gro off: {}".format(out.strip()))
    if QUEUES:
        rc, out = ethtool(["-L", iface, "combined", QUEUES])
        if rc != 0:
            say("nic: FAILED combined {}: {}".format(QUEUES, out.strip()))

    # Read back. `ethtool -k` prints "generic-receive-offload: on|off [fixed]";
    # `-l` prints the pre-set maximums then the current settings, so the
    # *last* Combined line is the one in force.
    rc, feats = ethtool(["-k", iface])
    gro_now = "?"
    for line in feats.splitlines():
        if line.strip().startswith("generic-receive-offload:"):
            gro_now = line.split(":", 1)[1].strip().split()[0]
    rc, chans = ethtool(["-l", iface])
    combined_now = "?"
    for line in chans.splitlines():
        if line.strip().startswith("Combined:"):
            combined_now = line.split(":", 1)[1].strip()
    say("nic: iface={} gro={} combined={}".format(iface, gro_now, combined_now))

BIN = os.path.join(WORK, "duckdb")
EXT = os.path.join(WORK, "httpfs.duckdb_extension")
DB = os.path.join(WORK, "tpch.duckdb")

TABLES = ["customer", "lineitem", "nation", "orders", "part", "partsupp", "region", "supplier"]


def fetch(url, dest):
    # Unsigned, through the S3 gateway endpoint: no IAM role, no credentials,
    # no internet route -- same grant shape as competitors/linux-s3's binary.
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as fh:
            shutil.copyfileobj(r, fh)
    except Exception as e:
        say("FAIL: could not fetch {}: {}".format(url, e))
        return False
    say("fetched {} ({} bytes)".format(os.path.basename(dest), os.path.getsize(dest)))
    return True


def run_sql(sql, json_out=True):
    args = [BIN, DB]
    if json_out:
        args.append("-json")
    args += ["-c", sql]
    r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout


# Aggregate of DuckDB's own HTTP request log, written to a file rather than
# to stdout: the run has to print a table too, and picking one JSON array out
# of several is a worse contract than a file whose only content is this.
#
# `window_ms` is first-request-start to last-request-end, so ms_sum/window_ms
# is the mean number of requests in flight -- the quantity that decides a
# latency-bound query, and the one no per-request average can show. It is the
# same arithmetic the miniOSv arm's net_ms/query_ms gives.
HTTP_STATS_SQL = """
COPY (
  WITH r AS (
    SELECT request.type AS type,
           epoch_us(request.start_time) AS t0_us,
           request.duration_ms AS ms
    FROM duckdb_logs_parsed('HTTP')
  )
  SELECT count(*) AS n,
         count(*) FILTER (WHERE type = 'GET') AS n_get,
         count(*) FILTER (WHERE type = 'HEAD') AS n_head,
         round(avg(ms), 2) AS ms_avg,
         round(quantile_cont(ms, 0.5), 2) AS ms_p50,
         round(quantile_cont(ms, 0.9), 2) AS ms_p90,
         round(quantile_cont(ms, 0.99), 2) AS ms_p99,
         min(ms) AS ms_min,
         max(ms) AS ms_max,
         sum(ms) AS ms_sum,
         round((max(t0_us + ms * 1000) - min(t0_us)) / 1000.0, 2) AS window_ms
  FROM r
) TO '{out}' (FORMAT json, ARRAY true)
"""


def http_profile(qn):
    """Re-run one query with the HTTP log on and report the request shape.

    A second pass, not the timed one: formatting a log record per request
    costs something, and the number the comparison plots must not carry it.
    Its own wall time is printed so the cost is visible rather than assumed.
    """
    out = os.path.join(WORK, "http-q{}.json".format(qn))
    if os.path.exists(out):
        os.remove(out)
    # Order matters twice over. `logging_storage` first because the CLI's
    # default sink is stdout, which would print a formatted record per request
    # into the middle of the results the driver parses -- and slow the run
    # down by doing it over a serial console. `enable_logging` last because
    # every SET between the two is itself a log record.
    prelude = (
        "LOAD '{ext}'; "
        "{threads}"
        "SET logging_storage='memory'; "
        "SET logging_level='debug'; "
        "SET logging_mode='ENABLE_SELECTED'; "
        "SET enabled_log_types='HTTP'; "
        "SET enable_logging=true; "
    ).format(ext=EXT, threads=thread_setting())
    sql = prelude + "PRAGMA tpch({});".format(qn) + HTTP_STATS_SQL.format(out=out)
    t0 = time.perf_counter()
    try:
        run_sql(sql, json_out=False)
    except Exception as e:
        say("HTTP STATS: FAILED {}".format(str(e).replace("\n", " ")[:300]))
        return
    ms = (time.perf_counter() - t0) * 1000
    try:
        with open(out) as fh:
            rows = json.load(fh)
    except Exception as e:
        say("HTTP STATS: FAILED unreadable {}: {}".format(out, e))
        return
    if not rows:
        say("HTTP STATS: FAILED no rows")
        return
    r = rows[0]
    # A window of zero would divide by zero, and means the log held one
    # instant's worth of requests -- report it rather than inventing a rate.
    window = r.get("window_ms") or 0
    conc = (r.get("ms_sum") or 0) / window if window else 0
    say(
        "HTTP STATS: q={} n={} get={} head={} ms_avg={} ms_p50={} ms_p90={} "
        "ms_p99={} ms_min={} ms_max={} ms_sum={} window_ms={} concurrency={:.2f} "
        "profile_ms={:.1f}".format(
            qn, r.get("n"), r.get("n_get"), r.get("n_head"), r.get("ms_avg"),
            r.get("ms_p50"), r.get("ms_p90"), r.get("ms_p99"), r.get("ms_min"),
            r.get("ms_max"), r.get("ms_sum"), r.get("window_ms"), conc, ms)
    )


def cpu_probe():
    """Run PROBE_STEPS in one process and print a `PROBE:` line per step.

    One process because the miniOSv arm is one: `dbgen` builds the tables the
    two query steps then read, so splitting them would measure a different
    thing. Timing comes from the CLI's own `.timer`, paired to steps by a
    `.print` marker ahead of each -- dot-commands are not timed, so the Nth
    marker is followed by exactly its own step's Run Time line, whatever the
    statements print in between.

    `:memory:`, not the TPC-H database: that one holds views over S3, and the
    point of this ladder is that nothing here touches the network.
    """
    # `pre` goes ahead of the marker, so the Run Time that follows the marker
    # is the step's own and not the SET's.
    script = [".timer on"]
    for name, pre, sql in PROBE_STEPS:
        if pre:
            script.append(pre + ";")
        script.append(".print PROBE_BEGIN {}".format(name))
        script.append(sql + ";")
    r = subprocess.run([BIN, ":memory:"], input="\n".join(script) + "\n",
                       capture_output=True, text=True, timeout=900)
    text = (r.stdout or "") + "\n" + (r.stderr or "")

    # Walk the output once: remember the last marker seen, and attribute the
    # next Run Time to it.
    pending = None
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("PROBE_BEGIN "):
            pending = line.split(None, 1)[1]
        elif pending and line.startswith("Run Time (s):"):
            m = re.search(r"real ([\d.]+)\s+user ([\d.]+)\s+sys ([\d.]+)", line)
            if m:
                say("PROBE: name={} ms={:.1f} user_ms={:.1f} sys_ms={:.1f}".format(
                    pending, float(m.group(1)) * 1000, float(m.group(2)) * 1000,
                    float(m.group(3)) * 1000))
                seen.add(pending)
            pending = None
    for name, _, _sql in PROBE_STEPS:
        if name not in seen:
            say("PROBE: name={} FAILED no timing".format(name))
    ok = len(seen)
    say("{}: cpuprobe ok={}/{}".format(
        "COMPLETE" if ok == len(PROBE_STEPS) else "INCOMPLETE", ok, len(PROBE_STEPS)))
    return 0 if ok == len(PROBE_STEPS) else 1


def thread_setting():
    """`SET threads=N;` or nothing, to prepend to a query run."""
    return "SET threads={}; ".format(int(THREADS)) if THREADS else ""


def setup_views():
    stmts = ["LOAD '{}';".format(EXT)]
    for t in TABLES:
        stmts.append(
            "CREATE VIEW {t} AS SELECT * FROM "
            "read_parquet('{ep}/tpch/sf{sf}/{t}.parquet');".format(t=t, ep=ENDPOINT, sf=SF)
        )
    run_sql(" ".join(stmts), json_out=False)


def fetch_answers():
    # tpch/parquet ship bundled in the release CLI -- no INSTALL, which is
    # good, because INSTALL needs $HOME and cloud-init's scripts-user module
    # runs with it unset ("Can't find the home directory at ''").
    out = run_sql(
        "LOAD '{}'; "
        "SELECT query_nr, answer FROM tpch_answers() WHERE scale_factor = {};".format(EXT, SF)
    )
    return {r["query_nr"]: r["answer"] for r in json.loads(out)}


def fields_match(a, b):
    """Numeric-tolerant: DuckDB's own JSON number formatting and the canned
    answer's pipe-text formatting don't always agree on trailing zeros for an
    otherwise-identical value (e.g. sum(l_quantity) as int vs x.00)."""
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return str(a) == b
    return abs(fa - fb) <= 1e-6 * max(1.0, abs(fa))


def result_matches(rows, answer_text):
    if not rows:
        return None  # nothing to key column order off; leave unchecked
    header = list(rows[0].keys())
    lines = answer_text.strip("\n").split("\n")
    want_header, want_rows = lines[0].split("|"), [ln.split("|") for ln in lines[1:]]
    if want_header != header or len(want_rows) != len(rows):
        return False
    for wr, r in zip(want_rows, rows):
        if len(wr) != len(header):
            return False
        if not all(fields_match(r[name], wf) for name, wf in zip(header, wr)):
            return False
    return True


def main():
    rc = 1
    ok = checked = matched = 0
    total_ms = 0.0
    try:
        os.makedirs(WORK, exist_ok=True)
        # cloud-init's scripts-user module runs with HOME set to '', not
        # unset -- setdefault() would not catch that, and DuckDB's IO layer
        # fails on the empty string rather than treating it as absent.
        if not os.environ.get("HOME"):
            os.environ["HOME"] = WORK
        say("tpch-linux: sf={}, {} quer{}, bucket {}".format(
            SF, len(QUERIES), "y" if len(QUERIES) == 1 else "ies", BUCKET))
        tune_nic()

        if not fetch(ENDPOINT + "/bin/duckdb-linux/duckdb", BIN):
            say("INCOMPLETE: duckdb binary unavailable")
            return 1
        os.chmod(BIN, 0o755)

        # Before httpfs: the ladder needs the binary and nothing else, and
        # fetching an extension it will not load only adds a way to fail.
        if CPU_PROBE:
            return cpu_probe()

        if not fetch(ENDPOINT + "/bin/duckdb-linux/httpfs.duckdb_extension", EXT):
            say("INCOMPLETE: httpfs extension unavailable")
            return 1

        setup_views()
        answers = fetch_answers()

        for qn in QUERIES:
            t0 = time.perf_counter()
            try:
                out = run_sql("LOAD '{}'; {}PRAGMA tpch({});".format(
                    EXT, thread_setting(), qn))
                rows = json.loads(out) if out.strip() else []
            except Exception as e:
                say("Q{:02d}: FAIL 0.0 ms: {}".format(qn, e))
                continue
            ms = (time.perf_counter() - t0) * 1000
            total_ms += ms
            ok += 1

            match = "unchecked"
            ans = answers.get(qn)
            if ans is not None:
                checked += 1
                m = result_matches(rows, ans)
                if m is True:
                    matched += 1
                    match = "yes"
                elif m is False:
                    match = "no"
            say("Q{:02d}: {:.1f} ms, {} rows, match={}".format(qn, ms, len(rows), match))

            if HTTP_LOG:
                http_profile(qn)

        say("")
        say("TPCH SUMMARY: ok={} total={} ms={:.1f} checked={} matched={}".format(
            ok, len(QUERIES), total_ms, checked, matched))
        complete = ok == len(QUERIES) and matched == checked
        say("{}: tpch sf={} ok={}/{}".format(
            "COMPLETE" if complete else "INCOMPLETE", SF, ok, len(QUERIES)))
        rc = 0 if complete else 1
        return rc
    finally:
        if LOCAL:
            say("rc={} — BENCH_LOCAL=1, staying up".format(rc))
        else:
            # Powering off purges the console, the only copy of the results.
            # The driver reaps us on COMPLETE/INCOMPLETE; this timer catches a
            # dead driver.
            say("rc={} — staying up for the console read".format(rc))
            subprocess.run(["sync"])
            subprocess.run(["shutdown", "-h", "+15"])


if __name__ == "__main__":
    sys.exit(main())
