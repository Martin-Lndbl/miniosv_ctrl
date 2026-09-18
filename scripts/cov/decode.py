#!/usr/bin/env python3
"""Console logs -> .profraw: the COV: lines cov.cc prints, checked for gaps.

    scripts/cov/decode.py out.profraw deploy.log [more logs ...]

The guest prints the dump twice and the console is read in snapshots, so
the passes (and any further logs) are merged line by line.
"""
import base64
import re
import struct
import sys
from pathlib import Path


def decode(texts: list[str]) -> bytes:
    lines: dict[int, str] = {}
    want = end = None
    for text in texts:
        for m in re.finditer(r"^COV:(\d+):([A-Za-z0-9+/=]+)\r?$", text, re.M):
            lines.setdefault(int(m.group(1)), m.group(2))
        if m := re.search(r"^COV BEGIN raw=(\d+)", text, re.M):
            want = int(m.group(1))
        if m := re.search(r"^COV END lines=(\d+)", text, re.M):
            end = int(m.group(1))
    if end is None or want is None:
        raise SystemExit("no complete COV dump in the logs")
    missing = [i for i in range(end) if i not in lines]
    if missing:
        raise SystemExit(f"{len(missing)} of {end} lines missing, first {missing[:8]}")
    rle = base64.b64decode("".join(lines[i] for i in range(end)))
    out = bytearray()
    i = 0
    while i < len(rle):
        if rle[i]:
            out.append(rle[i])
            i += 1
        else:
            out += bytes(struct.unpack_from("<I", rle, i + 1)[0])
            i += 5
    if len(out) != want:
        raise SystemExit(f"decoded {len(out)} bytes, the guest wrote {want}")
    return bytes(out)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    raw = decode([Path(p).read_text(errors="replace") for p in sys.argv[2:]])
    Path(sys.argv[1]).write_bytes(raw)
    print(f"{sys.argv[1]}: {len(raw)} bytes")
