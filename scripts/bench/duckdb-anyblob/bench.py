#!/usr/bin/env python3
"""Sweep TPC-H queries against DuckDB on Linux reading S3 through AnyBlob
(Durner, Leis, Neumann, VLDB 2023): the same DuckDB tree the miniOSv arm runs
(apps/miniduckdb), built as one static binary with httpfs's AnyBlob client
(apps/miniduckdb-httpfs/src/httpfs_anyblob_client.cpp) in place of curl.

    just bench competitors/duckdb-anyblob --sweep query=1,6 --sf 100 --threads 256 --pin auto

Everything but the binary is competitors/duckdb-linux's: the same instance
script, the same query loop, the same log lines, so the rows land in one CSV
shape with the Linux and miniOSv arms. `pin=auto` pins the bucket name to the
front-end the sweep resolved (the one the miniOSv image compiles in).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runner  # noqa: E402
from runner import ROOT  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "duckdb_linux_bench", Path(__file__).resolve().parents[1] / "duckdb-linux" / "bench.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
DuckdbLinux = _mod.DuckdbLinux

BENCH = "competitors/duckdb-anyblob"


class DuckdbAnyBlob(DuckdbLinux):
    name = "duckdb-anyblob"
    os_name = "anyblob"
    bench_path = BENCH
    # The instance script is duckdb-linux's, parametrised by the CONFIG below.
    scripts_dir = ROOT / "competitors/duckdb-linux/scripts"
    knobs = DuckdbLinux.knobs | {
        # AnyBlob daemons (threads with an io_uring each) and requests in
        # flight per daemon; the library caps the latter at 128. The paper's
        # numbers for a 100 Gbit/s instance are 12 and about 20; DuckDB's
        # synchronous reads never exceed its own thread count in flight.
        "workers": (None, int),
        "conns": (None, int),
        # Receive size per io_uring op; the library's default is 64 KiB.
        "chunk": (None, str),
    }
    defaults = DuckdbLinux.defaults | {"workers": 12, "conns": 128, "chunk": "64K", "pin": "auto"}
    instance_tag = "duckdb-anyblob-bench"

    def extra_conf(self, cfg: dict) -> dict:
        return {
            "BENCH_BIN_PREFIX": "bin/duckdb-anyblob",
            "BENCH_HTTPFS_BUILTIN": "1",
            "HTTPFS_CLIENT": "anyblob",
            "HTTPFS_ANYBLOB_THREADS": str(cfg.get("workers") or ""),
            "HTTPFS_ANYBLOB_CONCURRENCY": str(cfg.get("conns") or ""),
            "HTTPFS_ANYBLOB_CHUNK": str(cfg.get("chunk") or ""),
        }


def main() -> None:
    bench = DuckdbAnyBlob()
    argv = sys.argv[1:]
    if "--dry-run" not in argv:
        override = None
        if "--ami" in argv:
            override = argv[argv.index("--ami") + 1]
        bench.ami = bench.resolve_ami(override)
        print(f"ami      : {bench.ami}")
    runner.cli(bench)


if __name__ == "__main__":
    main()
