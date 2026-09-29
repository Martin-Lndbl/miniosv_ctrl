#!/usr/bin/env python3
"""Drop each query's first turn, so a plot may pool the rest.

The suites run every query three times in one boot -- cold, heated, warm.
Warming turns out to be worth almost nothing at sf=100: measured over two
boots the warm/cold ratio is 0.997 and 0.996 with a standard deviation of
0.15, and the warm turn was the slower one 10 times in 22 on both. A 0.3%
effect against 15% noise means the turns are samples of the same thing, so
pooling them is simply more data.

The first turn is still dropped, from every query alike. Q01's is the only
one that is genuinely cold -- 0.853 and 0.768 against its own warm turn, with
S3's first byte falling 38->27 and 42->24 ms -- because every later query
reads lineitem ranges Q01 has already warmed. Dropping only Q01's would leave
that query with fewer samples than the rest and a footnote to explain; taking
the first turn off all of them costs one sample per query and leaves every
query with the same six, treated identically.

A boot is identified by its deploy log, which every row of that run shares.

`query_s` is added alongside `query_ms`: sf=100 queries run for seconds, and
plot.py has no scale option, so the seconds have to exist as a column.

    scripts/bench/poolturns.py in.csv out.csv
"""
import csv
import sys
from collections import defaultdict

src, dst = sys.argv[1], sys.argv[2]
rows = list(csv.DictReader(open(src)))

first = {}
for i, r in enumerate(rows):
    if not r.get("query_seq"):
        continue
    k = (r.get("log"), r.get("query"))
    s = int(r["query_seq"])
    if k not in first or s < first[k][0]:
        first[k] = (s, i)
drop = {i for _, i in first.values()}
kept = [r for i, r in enumerate(rows) if i not in drop]

n = defaultdict(int)
for r in kept:
    n[r.get("query")] += 1
counts = sorted(set(n.values()))

for r in kept:
    r["query_s"] = f"{float(r['query_ms']) / 1000:.4f}" if r.get("query_ms") else ""

w = csv.DictWriter(open(dst, "w", newline=""), fieldnames=list(rows[0].keys()) + ["query_s"])
w.writeheader()
w.writerows(kept)
print(f"{len(rows)} rows -> {len(kept)} ({len(drop)} first turns dropped); "
      f"samples per query: {counts if len(counts) > 1 else counts[0]}")
