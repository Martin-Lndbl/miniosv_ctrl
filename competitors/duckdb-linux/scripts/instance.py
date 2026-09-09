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

        if not fetch(ENDPOINT + "/bin/duckdb-linux/duckdb", BIN):
            say("INCOMPLETE: duckdb binary unavailable")
            return 1
        os.chmod(BIN, 0o755)
        if not fetch(ENDPOINT + "/bin/duckdb-linux/httpfs.duckdb_extension", EXT):
            say("INCOMPLETE: httpfs extension unavailable")
            return 1

        setup_views()
        answers = fetch_answers()

        for qn in QUERIES:
            t0 = time.perf_counter()
            try:
                out = run_sql("LOAD '{}'; PRAGMA tpch({});".format(EXT, qn))
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
