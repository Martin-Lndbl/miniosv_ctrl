#!/usr/bin/env python3
"""Which parts of the network stack a TPC-H run executes.

    scripts/cov/run.py --tls 1 --sf 1 --queries 1-22 --out results/cov/tls
    scripts/cov/run.py --tls 0 --queries 1,6,9,18 --sf 10

Builds the image with MININET_COV=1 (mininet and its crates, the shim, the
ENA driver and DuckDB's client instrumented), deploys it, collects the
profile the guest prints at exit from the console log, and writes llvm-cov's
per-file report plus a list of every instrumented function with its hit
count under --out.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/bench"))
import runner  # noqa: E402

APP = ROOT / "apps/bench/duckdb-tpch"
SOURCES = ["modules/mininet", "drivers/enav2", "apps/miniduckdb/miniosv/http", "smoltcp-", "rustls-0", "ring-", "webpki-"]


def sh(cmd, **kw):
    print("+", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(cmd, check=True, **kw)


def queries(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += range(int(a), int(b or a) + 1)
    return out


def wait_for(path: Path, pattern: str, timeout: float, proc=None) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and (m := re.search(pattern, path.read_text(errors="replace"), re.M)):
            return m.group(1) if m.groups() else m.group(0)
        if proc is not None and proc.poll() is not None:
            return None
        time.sleep(3)
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tls", type=int, default=1)
    p.add_argument("--sf", default="1")
    p.add_argument("--queries", default="1-22")
    p.add_argument("--threads", type=int, default=0)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--conns", type=int, default=64)
    p.add_argument("--instance", default="c6in.8xlarge")
    p.add_argument("--miniosv", type=Path, default=ROOT / "miniosv", help="kernel tree to build in")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--profraw", type=Path, help="skip the run; report this profile against the tree's loader.elf")
    a = p.parse_args()
    out = a.out
    out.mkdir(parents=True, exist_ok=True)
    elf = a.miniosv / "build/release/loader.elf"

    if not a.profraw:
        bucket, region = os.environ["AWS_BUCKET"], os.environ["AWS_REGION"]
        ip = runner.target_ip()
        env = {**os.environ, "MININET_COV": "1", "MININET_HOST": f"{bucket}.s3.{region}.amazonaws.com",
               "MININET_ADDR": ip, "MININET_TLS": str(a.tls), "MININET_WORKERS": str(a.workers),
               "MININET_CONNS": str(a.conns)}
        sh(["make", "-C", str(a.miniosv), f"app={APP}", f"-j{os.cpu_count()}"], env=env,
           stdout=(out / "build.log").open("w"), stderr=subprocess.STDOUT)
        image = a.miniosv / "build/last/loader.img"
        thr = f" --threads {a.threads}" if a.threads else ""
        sh([sys.executable, str(a.miniosv / "scripts/setargs.py"), str(image),
            f"tpch --sf {a.sf}{thr} " + " ".join(map(str, queries(a.queries)))])

        deploy_log = out / "deploy.log"
        deploy = subprocess.Popen([str(a.miniosv / "scripts/aws-deploy.py"), str(image), region, a.instance,
                                   "--attach", "--subnet", os.environ["AWS_SUBNET"]], cwd=a.miniosv,
                                  stdout=deploy_log.open("wb"), stderr=subprocess.STDOUT)
        try:
            iid = wait_for(deploy_log, r"^Instance running: (i-[0-9a-f]+)", 900, deploy)
            if not iid:
                raise SystemExit(f"no instance; see {deploy_log}")
            print(f"instance {iid}; console -> {deploy_log}", flush=True)
            # The guest prints the dump twice; the console log is polled in 64 KB
            # snapshots and the decoder merges the passes.
            deadline = time.time() + 1800
            while time.time() < deadline and deploy_log.read_text(errors="replace").count("COV END") < 2:
                if deploy.poll() is not None:
                    raise SystemExit(f"deploy exited early; see {deploy_log}")
                time.sleep(5)
            time.sleep(10)
        finally:
            deploy.send_signal(signal.SIGTERM)
            deploy.wait()
        a.profraw = out / "profile.profraw"
        sh([sys.executable, str(ROOT / "scripts/cov/decode.py"), str(a.profraw), str(deploy_log)])

    profdata = out / "profile.profdata"
    sh(["llvm-profdata", "merge", str(a.profraw), "-o", str(profdata)])
    common = [str(elf), f"-instr-profile={profdata}", "-Xdemangler=llvm-cxxfilt"]
    with (out / "report.txt").open("w") as fh:
        sh(["llvm-cov", "report", *common, "-ignore-filename-regex=.*/(core|alloc|compiler_builtins|std)/.*"], stdout=fh)
    export = json.loads(subprocess.check_output(["llvm-cov", "export", "-format=text", "-skip-expansions", *common]))
    rows = []
    for f in export["data"][0]["functions"]:
        files = [x for x in f["filenames"] if any(s in x for s in SOURCES)]
        if not files:
            continue
        lines = sorted({r[0] for r in f["regions"]} | {r[2] for r in f["regions"]})
        rows.append((files[0], lines[0], lines[-1], f["count"], f["name"]))
    rows.sort()
    with (out / "functions.tsv").open("w") as fh:
        fh.write("file\tfrom\tto\thits\tfunction\n")
        for r in rows:
            fh.write("\t".join(map(str, r)) + "\n")
    never = [r for r in rows if r[3] == 0]
    print(f"{len(rows)} functions, {len(never)} never run; {out}/report.txt, {out}/functions.tsv")


if __name__ == "__main__":
    main()
