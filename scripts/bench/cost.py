#!/usr/bin/env python3
"""What a set of runs cost: S3 requests, transfer, and instance time.

    scripts/bench/cost.py results/tpch/*.csv

Three lines matter and only one of them is usually the big one:

  * **S3 GET** at $0.0004/1000. One TPC-H query at sf=100 is ~18k requests, so
    a 22-query suite is ~400k -- $0.16 a suite, which on that workload is the
    *majority* of the bill. A bandwidth bench issuing one GET per 32 MiB block
    is nothing by comparison.
  * **Transfer** is $0.00 as long as it stays in one AZ and goes through the
    S3 gateway endpoint. Printed anyway, next to what the same bytes would
    cost cross-AZ ($0.01/GB each way) or out an internet gateway ($0.09/GB),
    because those are one routing mistake away and ~1 TiB is not unusual here.
  * **Instances**, from the rows' own elapsed time plus a boot allowance,
    billed per second with a 60 s minimum. An estimate: the CSV does not
    record when the instance was launched or reaped.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

GET_PER_1000 = 0.0004
CROSS_AZ_PER_GB = 0.01      # each way
IGW_PER_GB = 0.09
BOOT_S = 90                 # deploy, boot, console read and teardown, roughly
MIN_BILL_S = 60


def spot_price(instance: str) -> float | None:
    try:
        import boto3
        import os
        c = boto3.client("ec2", region_name=os.environ.get("AWS_REGION", "eu-north-1"))
        h = c.describe_spot_price_history(
            InstanceTypes=[instance], ProductDescriptions=["Linux/UNIX"], MaxResults=1)
        return float(h["SpotPriceHistory"][0]["SpotPrice"])
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="+", type=Path)
    a = ap.parse_args()

    runs: dict[tuple[str, str], dict] = {}
    for f in a.csv:
        if not f.is_file():
            continue
        for r in csv.DictReader(f.open()):
            # One boot can write several rows (a query each); bill it once.
            key = (f.name, r.get("log") or r.get("instance_id") or "?")
            g = runs.setdefault(key, {"inst": r.get("instance", "?"), "req": 0,
                                      "bytes": 0, "ms": 0.0, "rows": 0})
            g["rows"] += 1
            for col, k in (("requests", "req"), ("body_bytes", "bytes")):
                try:
                    g[k] += int(float(r.get(col) or 0))
                except ValueError:
                    pass
            for col in ("query_ms", "elapsed_s"):
                try:
                    v = float(r.get(col) or 0)
                    g["ms"] += v * (1000 if col == "elapsed_s" else 1)
                    break
                except ValueError:
                    pass

    by_inst: dict[str, dict] = defaultdict(lambda: {"boots": 0, "req": 0, "bytes": 0, "s": 0.0})
    # Boots whose rows carry no request count at all. `requests` is a mininet
    # counter, so the whole Linux side of a paired TPC-H experiment lands here
    # and would otherwise be billed silently at $0 -- on a 22-query sf=100 run
    # that is ~367k GETs, $0.15 a boot, and a paired suite is understated by a
    # third. Counted and named rather than guessed at: the number of requests
    # is a property of the query and the scale factor, not of the stack, so the
    # other arm's count is a fair estimate -- but an estimate, so it is the
    # reader's to make.
    blind: dict[str, int] = defaultdict(int)
    for (f, _l), g in runs.items():
        b = by_inst[g["inst"]]
        b["boots"] += 1
        b["req"] += g["req"]
        b["bytes"] += g["bytes"]
        b["s"] += max(MIN_BILL_S, g["ms"] / 1000 + BOOT_S)
        if not g["req"]:
            blind[f] += 1

    tot_req = sum(b["req"] for b in by_inst.values())
    tot_b = sum(b["bytes"] for b in by_inst.values())
    tot_inst = 0.0
    print("%-16s %6s %12s %10s %10s %10s" % ("instance", "boots", "requests", "GiB", "inst-min", "$ compute"))
    for inst, b in sorted(by_inst.items()):
        p = spot_price(inst)
        c = (b["s"] / 3600) * p if p else 0.0
        tot_inst += c
        print("%-16s %6d %12d %10.1f %10.1f %10s"
              % (inst, b["boots"], b["req"], b["bytes"] / 2**30, b["s"] / 60,
                 ("$%.3f" % c) if p else "price?"))
    s3 = tot_req * GET_PER_1000 / 1000
    print()
    print("  S3 GET        %10d requests           $%.3f" % (tot_req, s3))
    print("  transfer      %10.1f GiB in-region     $0.000" % (tot_b / 2**30))
    print("                %10s (cross-AZ would be $%.2f, via IGW $%.2f)"
          % ("", tot_b / 1e9 * CROSS_AZ_PER_GB * 2, tot_b / 1e9 * IGW_PER_GB))
    print("  compute       %10.1f minutes           $%.3f" % (
        sum(b["s"] for b in by_inst.values()) / 60, tot_inst))
    print("  %-38s $%.3f" % ("TOTAL", s3 + tot_inst))
    if blind:
        n = sum(blind.values())
        print()
        print("  WARNING: %d boot(s) record no request count, so their S3 GETs are NOT" % n)
        print("           in the total above. The Linux arm of a paired TPC-H run is the")
        print("           usual cause -- `requests` is a mininet counter. At sf=100 over")
        print("           22 queries that is ~367k GETs a boot, about $0.15 each:")
        for f, k in sorted(blind.items(), key=lambda kv: -kv[1]):
            print("             %-44s %3d boot(s)" % (f, k))
    return 0


if __name__ == "__main__":
    sys.exit(main())
