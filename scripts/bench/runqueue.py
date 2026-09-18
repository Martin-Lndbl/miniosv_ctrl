#!/usr/bin/env python3
"""Queue experiments on spot instances, detached from the shell that asked.

    just queue miniosv-sf10-query linux-sf10-query-parity               # one after the other
    just queue miniosv-sf10-query linux-sf10-query-parity --interleave  # rep-major across them
    just queue miniosv-tls-100g --reps 1 --ttl 90m --dry-run            # the checks, no runner
    just queue-status
    just queue-stop

Every check runs before anything detaches: credentials, the bucket's region,
the subnet and its S3 gateway endpoint, each experiment's plan, the blob the
S3 benches read, no queue already running, no instance of ours already up.
Then a runner starts in its own session and outlives this shell. It runs the
experiments with `--market spot`, waits ten minutes and tries again whenever
no zone has a spot instance, and stops itself at the TTL (five hours unless
told otherwise, six at most): it ends the experiment it is in, terminates
every instance of ours launched since the queue began, and deregisters the
images they booted from. State and logs are under results/queue/<id>/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import boto3  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

import experiment  # noqa: E402
import runner  # noqa: E402

ROOT = runner.ROOT
QUEUES = ROOT / "results" / "queue"
LATEST = QUEUES / "latest"
# Name tags of everything the drivers launch; aws-deploy.py names its images
# and instances miniosv-<image>-<time>.
OUR_TAGS = ["miniosv-*", "linux-s3-bench", "duckdb-linux-bench"]
TTL_DEFAULT = 5 * 3600
TTL_MAX = 6 * 3600
SPOT_REFUSED = "spot requested but not provided"
# `aws login` sessions end after a lifetime AWS does not expose; when they
# do, nothing can launch or terminate, so the queue stops and says so.
CREDENTIALS_GONE = ("ExpiredToken", "LoginRefreshRequired", "refresh token has expired", "RequestExpired")


def duration(text: str) -> int:
    m = re.fullmatch(r"(\d+)\s*([smh]?)", text.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration: {text} (try 90m or 2h)")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def now() -> float:
    return time.time()


def stamp(t: float | None = None) -> str:
    return datetime.fromtimestamp(t or now(), timezone.utc).isoformat(timespec="seconds")


# -- checks ---------------------------------------------------------------


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_queue() -> dict | None:
    """The latest queue's state if its runner is still alive."""
    if not LATEST.is_file():
        return None
    try:
        state = json.loads((QUEUES / LATEST.read_text().strip() / "state.json").read_text())
    except (OSError, ValueError):
        return None
    if state.get("status") in ("running", "waiting for spot") and alive(int(state.get("pid", 0) or 0)):
        return state
    return None


def our_instances(since: float | None = None) -> list[dict]:
    r = runner.ec2().describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": OUR_TAGS},
            {"Name": "instance-state-name", "Values": ["pending", "running"]},
        ]
    )
    out = []
    for res in r["Reservations"]:
        for i in res["Instances"]:
            if since is None or i["LaunchTime"].timestamp() >= since:
                out.append(i)
    return out


def checks(a, xs: list[dict]) -> None:
    """Everything that would make the runner fail after the shell is gone."""
    missing = [k for k in experiment.REQUIRED_ENV if k not in os.environ]
    if missing:
        raise SystemExit(f"{', '.join(missing)} missing from the environment; run `just setup apps/bench/smoltcp-s3`")
    try:
        who = boto3.client("sts", region_name=os.environ["AWS_REGION"]).get_caller_identity()["Arn"]
    except (BotoCoreError, ClientError) as e:
        raise SystemExit(f"no usable AWS credentials: {e}") from e
    print(f"aws        : {who}")

    runner.check_bucket_region()
    print(f"bucket     : s3://{os.environ['AWS_BUCKET']} in {os.environ['AWS_REGION']}")

    ec2 = runner.ec2()
    try:
        subnet = ec2.describe_subnets(SubnetIds=[os.environ["AWS_SUBNET"]])["Subnets"][0]
    except ClientError as e:
        raise SystemExit(f"AWS_SUBNET {os.environ['AWS_SUBNET']} is not in {os.environ['AWS_REGION']}: {e}") from e
    endpoints = ec2.describe_vpc_endpoints(
        Filters=[
            {"Name": "vpc-id", "Values": [subnet["VpcId"]]},
            {"Name": "service-name", "Values": [f"com.amazonaws.{os.environ['AWS_REGION']}.s3"]},
            {"Name": "vpc-endpoint-type", "Values": ["Gateway"]},
        ]
    )["VpcEndpoints"]
    if not endpoints:
        raise SystemExit(f"{subnet['VpcId']} has no S3 gateway endpoint; the bucket policy would refuse every GET")
    print(f"subnet     : {subnet['SubnetId']} in {subnet['AvailabilityZone']}, endpoint {endpoints[0]['VpcEndpointId']}")

    s3 = boto3.client("s3", region_name=os.environ["AWS_REGION"])
    for x in xs:
        print(f"experiment : {experiment.qualified(x['path'])}")
        if x.get("deploy") or x.get("prep"):
            raise SystemExit(f"{experiment.qualified(x['path'])} is not a sweep; the queue runs sweeps only")
        if Path(x["bench"]).name in ("smoltcp-s3", "linux-s3"):
            try:
                s3.head_object(Bucket=os.environ["AWS_BUCKET"], Key="blob.bin")
            except ClientError as e:
                raise SystemExit(
                    f"s3://{os.environ['AWS_BUCKET']}/blob.bin is missing; run "
                    f"`just setup apps/bench/smoltcp-s3 < /dev/null` (every GET would be a 403)"
                ) from e
        cmd = [sys.executable, str(ROOT / "scripts/bench/experiment.py"), str(x["path"]), "--dry-run"]
        if a.reps:
            cmd += ["--reps", str(a.reps)]
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"{experiment.qualified(x['path'])} does not plan:\n{r.stdout}\n{r.stderr}")

    if q := running_queue():
        raise SystemExit(f"queue {q['id']} is still {q['status']} (pid {q['pid']}); `just queue-stop` first")
    if up := our_instances():
        names = ", ".join(f"{i['InstanceId']} ({i['InstanceType']})" for i in up)
        raise SystemExit(f"instances of ours are already up: {names}; a queue would share the build tree and the bucket with them")
    if a.ttl > TTL_MAX:
        raise SystemExit(f"--ttl {a.ttl}s is over the {TTL_MAX // 3600} h a queue may live")
    print(f"ttl        : {a.ttl // 60} min, retry every {a.retry_wait // 60} min while spot is refused")


# -- submit ---------------------------------------------------------------


def submit(a) -> int:
    xs = [experiment.load(n) for n in a.experiments]
    checks(a, xs)
    if a.dry_run:
        print("dry run: checks passed, nothing queued")
        return 0

    qid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    qdir = QUEUES / qid
    qdir.mkdir(parents=True)
    state = {
        "id": qid,
        "status": "starting",
        "pid": None,
        "started": now(),
        "ttl_s": a.ttl,
        "deadline": now() + a.ttl,
        "market": a.market,
        "interleave": a.interleave,
        "reps": a.reps,
        "retry_wait_s": a.retry_wait,
        "experiments": [
            {"name": experiment.qualified(x["path"]), "path": str(x["path"]), "status": "queued",
             "reps": a.reps or x["reps"], "attempts": 0, "spot_refusals": 0, "last": None,
             # The axis and its values, so an interleaved queue can run the
             # arms one point at a time: Q01 on both, then Q02 on both.
             "axis": x["axis"], "values": [str(p[x["axis"]]) for p in x["points"]]}
            for x in xs
        ],
        "log": str(qdir / "runner.log"),
        "events": [],
    }
    (qdir / "state.json").write_text(json.dumps(state, indent=1))
    LATEST.write_text(qid)

    log = (qdir / "runner.log").open("a")
    p = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "run", str(qdir)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # survives this shell, its terminal and its ssh session
        env=os.environ,
    )
    print(f"\nqueued {qid} as pid {p.pid}; it stops itself by {stamp(state['deadline'])}")
    print(f"  just queue-status         # progress\n  just queue-stop           # end it and clean up\n  tail -f {qdir / 'runner.log'}")
    return 0


# -- the runner -----------------------------------------------------------


class Runner:
    def __init__(self, qdir: Path):
        self.qdir = qdir
        self.state = json.loads((qdir / "state.json").read_text())
        self.child: subprocess.Popen | None = None
        self.stopping = False
        self.lock = threading.Lock()

    def save(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)
            self.state["updated"] = now()
            tmp = self.qdir / "state.json.tmp"
            tmp.write_text(json.dumps(self.state, indent=1))
            os.replace(tmp, self.qdir / "state.json")

    def event(self, msg: str) -> None:
        line = f"{stamp()} {msg}"
        print(line, flush=True)
        with self.lock:
            self.state["events"].append(line)

    def deadline(self) -> float:
        return self.state["deadline"]

    def run_one(self, x: dict, reps: int, plot: bool, only: str | None = None) -> str:
        """Run one experiment to `reps`, or just the point `only` (axis=value),
        retrying while spot is refused. Returns done | failed | expired | stopped."""
        cmd = [sys.executable, str(ROOT / "scripts/bench/experiment.py"), x["path"],
               "--reps", str(reps), "--market", self.state["market"]]
        if only:
            cmd += ["--only", only]
        if not plot:
            cmd.append("--no-plot")
        log = self.qdir / (Path(x["path"]).stem + ".log")
        while True:
            if self.stopping:
                return "stopped"
            if now() >= self.deadline():
                return "expired"
            x["attempts"] += 1
            x["status"] = "running"
            self.save(status="running")
            with log.open("a") as fh:
                fh.write(f"\n===== {stamp()} {x['name']} --reps {reps} attempt {x['attempts']} =====\n")
                fh.flush()
                self.child = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL,
                                              stdout=fh, stderr=subprocess.STDOUT,
                                              start_new_session=True, env=os.environ)
                while self.child.poll() is None:
                    if self.stopping or now() >= self.deadline():
                        self.end_child()
                        return "stopped" if self.stopping else "expired"
                    time.sleep(5)
            rc = self.child.returncode
            self.child = None
            if rc == 0:
                x["status"] = "done"
                return "done"
            tail = log.read_text(errors="replace")[-4000:]
            if any(m in tail for m in CREDENTIALS_GONE):
                x["status"] = "credentials expired"
                x["last"] = tail[-600:]
                return "credentials"
            if SPOT_REFUSED in tail:
                x["spot_refusals"] += 1
                x["status"] = "waiting for spot"
                self.save(status="waiting for spot")
                self.event(f"{x['name']}: no spot in any zone ({x['spot_refusals']}x); retrying in {self.state['retry_wait_s'] // 60} min")
                until = min(now() + self.state["retry_wait_s"], self.deadline())
                while now() < until and not self.stopping:
                    time.sleep(5)
                continue
            x["status"] = "failed"
            x["last"] = tail[-600:]
            return "failed"

    def end_child(self) -> None:
        """The experiment, its driver, and the deploy it may have running.
        aws-deploy.py tears its own instance, image and snapshot down on
        SIGINT, so it gets that first and 90 s to do it."""
        if not self.child:
            return
        pids = descendants(self.child.pid)
        deploys = [p for p, cmd in pids if "aws-deploy.py" in cmd]
        for p in deploys:
            try:
                os.killpg(os.getpgid(p), signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                pass
        for p, _ in pids:
            if p not in deploys:
                try:
                    os.kill(p, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        try:
            os.killpg(os.getpgid(self.child.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        end = now() + 90
        while now() < end and any(alive(p) for p in deploys):
            time.sleep(2)
        self.child = None

    def sweep(self) -> None:
        """Whatever of ours is still up from this queue's window."""
        since = self.state["started"] - 60
        ec2 = runner.ec2()
        try:
            up = our_instances(since)
            if up:
                ids = [i["InstanceId"] for i in up]
                ec2.terminate_instances(InstanceIds=ids)
                self.event(f"terminated {', '.join(ids)}")
            imgs = ec2.describe_images(Owners=["self"], Filters=[{"Name": "name", "Values": ["miniosv-*"]}])["Images"]
            for im in imgs:
                created = datetime.fromisoformat(im["CreationDate"].replace("Z", "+00:00")).timestamp()
                if created < since:
                    continue
                ec2.deregister_image(ImageId=im["ImageId"])
                for bdm in im.get("BlockDeviceMappings", []):
                    if snap := bdm.get("Ebs", {}).get("SnapshotId"):
                        try:
                            ec2.delete_snapshot(SnapshotId=snap)
                        except ClientError:
                            pass
                self.event(f"deregistered {im['ImageId']} ({im['Name']})")
        except (BotoCoreError, ClientError) as e:
            self.event(f"WARN: sweep failed: {e}; check the console for instances tagged {OUR_TAGS}")

    def main(self) -> int:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stopping", True))
        self.save(pid=os.getpid(), status="running")
        self.event(f"queue {self.state['id']} started, ttl {self.state['ttl_s'] // 60} min, market {self.state['market']}")
        runner.notify(f"{len(self.state['experiments'])} experiment(s), ttl {self.state['ttl_s'] // 60} min",
                      title=f"queue {self.state['id']} started", tags="hourglass")

        xs = self.state["experiments"]
        verdict = "done"
        if self.state["interleave"]:
            # Rep-major, and within a rep point-major across the arms: the
            # i-th point of every experiment before the (i+1)-th of any, so a
            # comparison exists after the first point and drift lands on both.
            most = max(x["reps"] for x in xs)
            longest = max(len(x["values"]) for x in xs)
            for k in range(1, most + 1):
                for i in range(longest):
                    for x in xs:
                        if x["status"] == "failed" or k > x["reps"] or i >= len(x["values"]):
                            continue
                        only = f"{x['axis']}={x['values'][i]}"
                        self.event(f"{x['name']} {only} rep {k}")
                        v = self.run_one(x, k, plot=False, only=only)
                        if v in ("expired", "stopped", "credentials"):
                            verdict = v
                            break
                        if v == "failed":
                            self.event(f"{x['name']} FAILED at {only}; see its log")
                    if verdict != "done":
                        break
                if verdict != "done":
                    break
            if verdict == "done":
                for x in xs:  # everything is done: this pass only plots
                    if x["status"] == "done":
                        self.run_one(x, x["reps"], plot=True)
        else:
            for x in xs:
                self.event(f"{x['name']}")
                v = self.run_one(x, x["reps"], plot=True)
                if v in ("expired", "stopped", "credentials"):
                    verdict = v
                    break
                if v == "failed":
                    self.event(f"{x['name']} FAILED; see its log")

        if verdict == "credentials":
            # No sweep is possible without credentials; the run in progress
            # already failed before launching, so nothing is up.
            self.event("credentials expired: the aws login session ended; `aws login`, then `just queue` again -- the CSVs resume")
            self.end_child()
        elif verdict in ("expired", "stopped"):
            self.event(f"{verdict}: ending the run in progress and sweeping")
            self.end_child()
            self.sweep()
        if verdict in ("expired", "stopped", "credentials"):
            for x in xs:
                if x["status"] in ("running", "waiting for spot", "queued"):
                    x["status"] = verdict
        elif any(x["status"] == "failed" for x in xs):
            verdict = "failed"
        self.save(status=verdict, finished=now())
        summary = ", ".join(f"{x['name']} {x['status']}" for x in xs)
        self.event(f"queue {verdict}: {summary}")
        runner.notify(summary, title=f"queue {self.state['id']} {verdict}",
                      tags="white_check_mark" if verdict == "done" else "warning",
                      priority="default" if verdict == "done" else "high")
        return 0 if verdict == "done" else 1


def descendants(pid: int) -> list[tuple[int, str]]:
    """(pid, cmdline) of every process below `pid`, from /proc."""
    kids: dict[int, list[int]] = {}
    cmds: dict[int, str] = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            cmds[int(d.name)] = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(d.name))
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        for k in kids.get(p, []):
            out.append((k, cmds.get(k, "")))
            todo.append(k)
    return out


# -- status / stop --------------------------------------------------------


def latest_state() -> dict:
    if not LATEST.is_file():
        raise SystemExit("no queue has run yet")
    qdir = QUEUES / LATEST.read_text().strip()
    return json.loads((qdir / "state.json").read_text())


def status(_a) -> int:
    s = latest_state()
    live = alive(int(s.get("pid") or 0))
    print(f"queue {s['id']}: {s['status']}{'' if live else ' (runner gone)'}, pid {s.get('pid')}")
    print(f"  started {stamp(s['started'])}, deadline {stamp(s['deadline'])}, market {s['market']}, "
          f"{'interleaved' if s['interleave'] else 'sequential'}")
    for x in s["experiments"]:
        extra = f", {x['spot_refusals']} spot refusal(s)" if x["spot_refusals"] else ""
        print(f"  {x['name']:40s} {x['status']:16s} attempts {x['attempts']}{extra}")
    for line in s["events"][-8:]:
        print(f"  {line}")
    print(f"  log: {s['log']}")
    return 0


def stop(_a) -> int:
    s = latest_state()
    pid = int(s.get("pid") or 0)
    if not alive(pid):
        print(f"queue {s['id']} is not running ({s['status']})")
        return 0
    os.kill(pid, signal.SIGTERM)
    print(f"asked queue {s['id']} (pid {pid}) to stop; it ends the run in progress and sweeps its instances")
    for _ in range(60):
        time.sleep(2)
        if not alive(pid):
            break
    return status(_a)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit", help="check, then detach a runner")
    s.add_argument("experiments", nargs="+")
    s.add_argument("--interleave", action="store_true", help="rep-major across the experiments, so drift lands on all")
    s.add_argument("--reps", type=int, default=None, help="overrides every experiment's reps")
    s.add_argument("--ttl", type=duration, default=TTL_DEFAULT, help="how long the queue may live (default 5h, max 6h)")
    s.add_argument("--retry-wait", type=duration, default=600, help="between spot attempts (default 10m)")
    s.add_argument("--market", choices=runner.MARKETS, default="spot")
    s.add_argument("--dry-run", action="store_true", help="the checks and the plan, no runner")
    s.set_defaults(fn=submit)
    r = sub.add_parser("run", help=argparse.SUPPRESS)
    r.add_argument("qdir")
    r.set_defaults(fn=lambda a: Runner(Path(a.qdir)).main())
    sub.add_parser("status").set_defaults(fn=status)
    sub.add_parser("stop").set_defaults(fn=stop)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
