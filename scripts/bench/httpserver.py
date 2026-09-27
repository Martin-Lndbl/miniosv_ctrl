"""The static file server an HTTP load bench dials: one AL2023 instance running
competitors/nginx-static, launched before a point's runs and terminated after
them. Each arm of a comparison gets one of its own, launched the same way, so
the arms differ only in the client.

On-demand, in the zone where the client's spot request scores best: the client
is pinned to the server's zone (same-zone traffic is free and a hop shorter),
and a zone with one spot slot must give it to the client, not the server.

A bench starts it in build() (once per point) and stops it at the next build
or at exit; the row records the server's id and zone. Its "Instance running:"
line is printed like a client's so a queue's sweep recognises it as ours.
"""
from __future__ import annotations

import atexit
import gzip
import json
import os
import re
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

import runner
from runner import ROOT, ec2

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
            ec2().terminate_instances(InstanceIds=[self.iid])
            print(f"  server {self.iid} terminated", flush=True)
        except Exception as e:  # the sweep catches what this misses
            print(f"    WARN: terminate {self.iid}: {e}", flush=True)
        self.iid = self.ip = None
