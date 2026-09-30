#!/usr/bin/env python3
"""A static nginx that outlives one experiment, for a whole suite to dial.

    scripts/bench/nginx-server.py start --client r6in.32xlarge
    scripts/bench/nginx-server.py status
    scripts/bench/nginx-server.py stop

Why: `server = "<type>"` in an experiment launches one nginx a point and tears
it down after, which is right for a single comparison and wrong for a sweep.
Two r6in.32xlarge is 256 of an account's 300 spot vCPUs, so the per-point
server and the next point's client fight for the same quota -- and a refused
client leaves the server it just launched to drain, which refuses the retry in
turn. Measured 2026-09-30: four attempts at one point, four wasted server
launches, no data.

The zone is picked by the **client's** spot odds, not the server's, because the
client is pinned to wherever the server landed (same-zone traffic is free and a
hop shorter). Pass --client so that ranking is the right one; the harness then
reports the zone to pin experiments to.

State lives in results/http/nginx-server.json so `status` and `stop` work from
any shell. `start` deliberately unregisters HttpServer's atexit hook -- the
whole point is that it survives the process.
"""
from __future__ import annotations

import argparse
import atexit
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpserver  # noqa: E402
from runner import ROOT, ec2  # noqa: E402

STATE = ROOT / "results" / "http" / "nginx-server.json"


def load() -> dict | None:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return None


def cmd_start(a) -> int:
    if (old := load()) and live(old["iid"]):
        print(f"already running: {old['iid']} at {old['ip']} ({old['zone']})")
        print("  stop it first, or use it as it is")
        return 1
    logdir = ROOT / "results" / "http" / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    srv = httpserver.HttpServer(a.instance, a.market, logdir, size=a.size,
                                workers=a.workers, client=a.client or a.instance)
    ip = srv.start()
    # Without this the server dies with this process, which is the one thing a
    # long-living server must not do.
    atexit.unregister(srv.stop)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(
        {"iid": srv.iid, "ip": ip, "zone": srv.zone, "instance": a.instance, "size": a.size}, indent=2))
    print(f"\nnginx up: {srv.iid} at {ip} in {srv.zone}")
    print("\nrun experiments against it with:")
    print(f"    BENCH_ZONE={srv.zone} just reproduce <experiment> --target-ip {ip}")
    print("  and `server` left unset in the experiment, or it will launch its own.")
    print(f"\nstop it with: scripts/bench/nginx-server.py stop   (it bills until you do)")
    return 0


def live(iid: str) -> bool:
    try:
        st = ec2().describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]["State"]["Name"]
    except Exception:
        return False
    return st in ("pending", "running")


def cmd_status(a) -> int:
    s = load()
    if not s:
        print("no server recorded")
        return 1
    print(f"{s['iid']} at {s['ip']} in {s['zone']} ({s['instance']}, blob {s['size']}): "
          f"{'RUNNING' if live(s['iid']) else 'gone'}")
    return 0


def cmd_stop(a) -> int:
    s = load()
    if not s:
        print("no server recorded")
        return 1
    import runner
    runner.terminate(ec2(), [s["iid"]])   # waits for `terminated`, frees the quota
    print(f"terminated {s['iid']}")
    STATE.unlink(missing_ok=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("start")
    st.add_argument("--instance", default="r6in.32xlarge", help="the server's type")
    st.add_argument("--client", default=None,
                    help="the client's type, whose spot odds pick the zone (default: same as --instance)")
    st.add_argument("--size", default="50G", help="blob size, laid in tmpfs -- must fit the server's RAM")
    st.add_argument("--workers", default="auto")
    st.add_argument("--market", default="spot", choices=["spot", "on-demand", "spot-or-on-demand"])
    st.set_defaults(fn=cmd_start)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
