"""The static file server an HTTP load bench dials: one AL2023 instance running
competitors/nginx-static, launched before a point's runs and terminated after
them. Each arm of a comparison gets one of its own, launched the same way, so
the arms differ only in the client.

Spot like everything larger than a .large (BENCH_SERVER_MARKET overrides), in
the zone where the client's spot request scores best: the client is pinned to
the server's zone (same-zone traffic is free and a hop shorter).

A bench starts it in build() (once per point) and stops it at the next build
or at exit; the row records the server's id and zone. Its "Instance running:"
line is printed like a client's so a queue's sweep recognises it as ours.
"""
from __future__ import annotations

import atexit
import gzip
import ipaddress
import json
import os
import re
import subprocess
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

import runner
from runner import ROOT, ec2

BIN_PREFIX = "bin/nginx-static"   # competitors/nginx-static/justfile
SSM_AMI = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64"
SCRIPT = ROOT / "competitors/nginx-static/scripts/server.py"
TAG = "miniosv-nginx-server"  # under the queue's miniosv-* glob
READY_S = 600  # the EC2 console lags launch by a minute or two


def spot_ranked_zones(c, instance: str) -> list[str]:
    """The VPC's zones, best spot placement score for one such instance first;
    the subnet's own order when the scores cannot be read."""
    names = [z for _, z in runner.spot_subnets(c, os.environ["AWS_SUBNET"])]
    if order := os.environ.get("BENCH_ZONES"):  # by hand, when a zone keeps refusing despite its score
        return [z for z in order.split(",") if z in names] + [z for z in names if z not in order]
    try:
        by_id = {z["ZoneId"]: z["ZoneName"] for z in c.describe_availability_zones()["AvailabilityZones"]}
        scores = c.get_spot_placement_scores(InstanceTypes=[instance], TargetCapacity=1, SingleAvailabilityZone=True,
                                             RegionNames=[os.environ["AWS_REGION"]])["SpotPlacementScores"]
        score = {by_id.get(s.get("AvailabilityZoneId"), ""): s["Score"] for s in scores}
        names.sort(key=lambda z: -score.get(z, 0))
        print(f"    spot placement scores for {instance}: " + ", ".join(f"{z}={score.get(z, '?')}" for z in names), flush=True)
    except ClientError as e:
        print(f"    spot placement scores unavailable ({e.response.get('Error', {}).get('Code')}); subnet order", flush=True)
    return names


def verify_external(target_ip: str | None, zone: str | None) -> None:
    """Check a long-living server is where the run is about to assume it is.

    A server launched per point put its own `zone` in the row, read off the
    object that launched it. Dialling a long-living one, the zone is whatever
    BENCH_ZONE says -- an assertion, not an observation -- so a stale or
    mistyped value pins the client to it *and* records the same string. The
    same-AZ check then compares a claim with itself, passes, and the traffic
    crosses an AZ at $0.01/GB each way. A public target is worse: the internet
    gateway bills $0.09/GB and both ends really are in one zone, so nothing
    downstream notices at all.

    So this resolves the claims against the instance itself, before anything
    launches. It only fires for a private target -- an S3 front-end is public
    and is not ours to check.
    """
    if not target_ip:
        return
    try:
        private = ipaddress.ip_address(target_ip).is_private
    except ValueError:
        raise SystemExit(f"--target-ip {target_ip!r} is not an address")

    state = ROOT / "results" / "http" / "nginx-server.json"
    try:
        rec = json.loads(state.read_text())
    except (OSError, ValueError):
        rec = None

    if not private:
        if rec:
            print(f"    WARN: dialling public {target_ip} while {rec['iid']} is recorded as our "
                  f"nginx at {rec['ip']} -- egress over the internet gateway bills $0.09/GB", flush=True)
        return

    if not rec:
        raise SystemExit(
            f"--target-ip {target_ip} is private but no long-living server is recorded.\n"
            f"  start one with scripts/bench/nginx-server.py start, or pass the type as `server`.")

    try:
        inst = ec2().describe_instances(InstanceIds=[rec["iid"]])["Reservations"][0]["Instances"][0]
    except Exception as e:
        raise SystemExit(f"cannot read the recorded server {rec['iid']}: {e}")

    state_name = inst["State"]["Name"]
    real_ip, real_zone = inst.get("PrivateIpAddress"), inst["Placement"]["AvailabilityZone"]
    if state_name != "running":
        raise SystemExit(f"the recorded nginx {rec['iid']} is {state_name}, not running")
    if real_ip != target_ip:
        raise SystemExit(f"--target-ip {target_ip} is not {rec['iid']} (that instance is {real_ip})")
    if not zone:
        raise SystemExit(f"BENCH_ZONE is unset; {rec['iid']} is in {real_zone} and the client must be pinned there")
    if zone != real_zone:
        raise SystemExit(
            f"BENCH_ZONE={zone} but {rec['iid']} is in {real_zone}: the client would be pinned to the "
            f"wrong zone and every byte billed at $0.01/GB each way")
    print(f"    preflight: nginx {rec['iid']} running at {real_ip} in {real_zone}, client pinned there", flush=True)


_BIN_READY = False


def ensure_binary() -> None:
    """Upload competitors/nginx-static if the bucket has no nginx.

    The client benches get this for free -- `Bench.build` shells out to
    `just setup <bench>` -- but the server is launched by the *client's* driver
    and nothing was building it. A bucket that had never had a
    `just setup competitors/nginx-static` (a fresh one, or one from before a
    region move) therefore produced a server whose user-data died on
    `HTTP Error 403: Forbidden` fetching bin/nginx-static/nginx, reported only
    as "nginx server never became ready". Checked once a process, and the
    upload is skipped when the object is already there.
    """
    global _BIN_READY
    if _BIN_READY:
        return
    key = f"{BIN_PREFIX}/nginx"  # matches competitors/nginx-static/justfile
    try:
        boto3.client("s3", region_name=os.environ["AWS_REGION"]).head_object(
            Bucket=os.environ["AWS_BUCKET"], Key=key)
        _BIN_READY = True
        return
    except ClientError:
        pass
    print(f"    s3://{os.environ['AWS_BUCKET']}/{key} missing; just setup competitors/nginx-static",
          flush=True)
    r = subprocess.run(["just", "setup", "competitors/nginx-static"],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"nginx-static setup failed:\n{r.stdout}\n{r.stderr}")
    print("    " + "\n    ".join(r.stdout.strip().splitlines()[-3:]), flush=True)
    _BIN_READY = True


class HttpServer:
    def __init__(self, instance: str, market: str, logdir: Path, size: str = "10G", workers: str = "auto",
                 client: str | None = None):
        self.instance, self.market, self.logdir = instance, market, logdir
        self.size, self.workers = size, workers
        self.client = client or instance  # whose spot odds pick the zone
        self.iid: str | None = None
        self.ip: str | None = None
        self.zone: str | None = None
        atexit.register(self.stop)

    def user_data(self) -> bytes:
        conf = {
            "AWS_BUCKET": os.environ["AWS_BUCKET"],
            "AWS_REGION": os.environ["AWS_REGION"],
            "BENCH_OBJECT_SIZE": self.size,
            "BENCH_SERVER_WORKERS": self.workers,
        }
        body = SCRIPT.read_text()
        body = body.split("\n", 1)[1] if body.startswith("#!") else body
        return gzip.compress(f"#!/usr/bin/env python3\nCONFIG = {json.dumps(conf)}\n{body}".encode())

    def start(self) -> str:
        ensure_binary()
        ami = boto3.client("ssm", region_name=os.environ["AWS_REGION"]).get_parameter(Name=SSM_AMI)["Parameter"]["Value"]
        c = ec2()
        kwargs = dict(
            ImageId=ami, InstanceType=self.instance, MinCount=1, MaxCount=1,
            SubnetId=os.environ["AWS_SUBNET"], UserData=self.user_data(),
            InstanceInitiatedShutdownBehavior="terminate",
            TagSpecifications=[{"ResourceType": "instance",
                                "Tags": [{"Key": "Name", "Value": TAG}, {"Key": "bench", "Value": "nginx-static"}]}],
        )
        for zone in spot_ranked_zones(c, self.client):
            try:
                r, market, zone = runner.launch(c, kwargs, self.market, zone=zone)
                break
            except (ClientError, SystemExit) as e:
                print(f"    server not provided in {zone} ({e})", flush=True)
        else:  # the queue's retry phrase: nothing else would help
            raise SystemExit("spot requested but not provided: no zone would launch the server")
        self.iid, self.zone = r["Instances"][0]["InstanceId"], zone
        print(f"  Instance running: {self.iid} ({self.instance}, {zone}, {market}) [nginx server]", flush=True)
        self.logdir.mkdir(parents=True, exist_ok=True)
        log = self.logdir / f"server-{self.iid}.log"
        text, deadline = "", time.time() + READY_S
        while time.time() < deadline:
            time.sleep(10)
            try:
                got = c.get_console_output(InstanceId=self.iid, Latest=True).get("Output", "") or ""
            except Exception:
                got = ""
            if len(got) > len(text):
                text = got
                log.write_text(text)
            if m := re.search(r"SERVER READY ip=([\d.]+)", text):
                self.ip = m.group(1)
                break
            if "SERVER FAILED" in text:
                break
        if not self.ip:
            self.stop()
            raise SystemExit(f"nginx server never became ready; see {log}")
        print(f"  server ready at {self.ip} ({zone})", flush=True)
        return self.ip

    def stop(self) -> None:
        if not self.iid:
            return
        try:
            runner.terminate(ec2(), [self.iid])
            print(f"  server {self.iid} terminated", flush=True)
        except Exception as e:  # the sweep catches what this misses
            print(f"    WARN: terminate {self.iid}: {e}", flush=True)
        self.iid = self.ip = None
