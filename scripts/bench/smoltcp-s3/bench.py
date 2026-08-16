#!/usr/bin/env python3
"""Sweep one compile-time knob of apps/bench/smoltcp-s3, one CSV row per run.

    just bench apps/bench/smoltcp-s3 --sweep conns=1,2,3,4,6,8,12,16,24
    just bench apps/bench/smoltcp-s3 --sweep workers=1,2,4,8 --conns 4
    just bench apps/bench/smoltcp-s3 --sweep block=16M,64M,256M --conns 4 --reps 1

Knobs are compiled in, so each axis value needs its own build; reps reuse it.
Non-axis knobs stay fixed and are recorded per row, so block size no longer
shrinks as parallelism rises and a throughput-vs-workers curve is only that.

Sweep machinery is in ../runner.py, shared with competitors/linux-s3.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runner                                                    # noqa: E402
from runner import ROOT, Bench, ec2, parse, size                 # noqa: E402

BENCH = "apps/bench/smoltcp-s3"


class SmoltcpS3(Bench):
    name = "smoltcp-s3"
    knobs = {"workers": ("BENCH_WORKERS", size),
             "conns": ("BENCH_CONNS_PER_WORKER", size),
             "block": ("BENCH_BLOCK_SIZE", size)}
    defaults = {"workers": 8, "conns": 24, "block": 128 << 20}
    # Created only by miniosv/scripts/aws-deploy.py.
    instance_tag = "miniosv-loader-*"
    # 50 Gbps sustained; c7i.8xlarge caps at 12.5 and the bench reaches 11.85
    # there, so its curve is clipped by EC2's allowance rather than by the stack.
    default_instance = "c6in.8xlarge"
    max_vm_seconds = 110            # keep every run well under two minutes

    # field -> (pattern, cast). The guest prints these; see osv_app_main().
    metrics = {
        "workers_actual": (r"^rss: (\d+) queues", int),
        "gbps": (r"AGGREGATE:.*?, ([\d.]+) Gbps", float),
        "mb_per_s": (r"AGGREGATE:.*?=> ([\d.]+) MB/s", float),
        "elapsed_s": (r"AGGREGATE: [\d.]+ MiB in ([\d.]+) s", float),
        "conns_clean": (r"^connections\s+: (\d+)/", int),
        "conns_total": (r"^connections\s+: \d+/(\d+)", int),
        "syn_retries": (r"^syn retries\s+: (\d+)", int),
        "misrouted": (r"^misrouted rx\s+: (\d+)", int),
        "setup_ms": (r"^setup\s+: (\d+) ms", int),
        "bytes": (r"\((\d+) bytes\)", int),
        "instance_id": (r"Instance running: (i-[0-9a-f]+)", str),
    }

    def build(self, cfg: dict, ip: str) -> None:
        """Bake this point's constants in; build.rs marks each knob
        rerun-if-env-changed so cargo rebuilds when one moves. Must run inside
        the devshell, whose `set -a; . .env` shellHook would otherwise win."""
        env = {**os.environ, "AWS_TARGET_IP": ip,
               **{self.knobs[k][0]: str(v) for k, v in cfg.items()}}
        r = subprocess.run(["just", "build", BENCH, "-j16"],
                           cwd=ROOT, env=env, capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"build failed for {cfg}:\n{r.stdout}\n{r.stderr}")

    def run_once(self, instance: str, logdir: Path, cfg: dict, ip: str) -> dict:
        """Deploy, wait for the guest's verdict, terminate, parse. Termination is
        an API call: a signal can resolve to the driver's own pgid and orphan a
        billing instance."""
        logdir.mkdir(parents=True, exist_ok=True)
        log = logdir / f"deploy-{instance}-{int(time.time())}.log"
        if up := self.live():
            raise SystemExit(f"refusing to launch, instance still up: {up}")

        with log.open("w") as fh:
            p = subprocess.Popen(["just", "deploy", instance], cwd=ROOT,
                                 stdout=fh, stderr=subprocess.STDOUT,
                                 start_new_session=True)
            iid = None
            for _ in range(1500):       # wait for the instance to exist
                if m := re.search(r"Instance running: (i-[0-9a-f]+)",
                                  log.read_text(errors="replace")):
                    iid = m.group(1)
                    break
                if p.poll() is not None:
                    break
                time.sleep(1)
            if iid:                     # billing starts here: cap it
                for _ in range(self.max_vm_seconds):
                    if re.search(r"^(COMPLETE|INCOMPLETE):",
                                 log.read_text(errors="replace"), re.M):
                        break
                    if p.poll() is not None:
                        break
                    time.sleep(1)
                ec2().terminate_instances(InstanceIds=[iid])
            # SIGINT is aws-deploy.py's teardown: it deregisters the AMI and
            # deletes the snapshot itself. Signal the GROUP, not p — p is
            # `just`, which does not forward it, so aws-deploy.py never saw it
            # and no deploy log ever contained "Deregistering AMI".
            # start_new_session makes the group exactly this deploy.
            #
            # The 75s is a ceiling, not a delay: wait() returns on exit, and
            # the instance is already terminating by the time we get here, so
            # aws-deploy's own 30x2s poll should break on its first iteration.
            # Hitting the ceiling means killing it mid-teardown, which leaks an
            # AMI and a snapshot per run, so say so rather than swallow it.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(p.pid), signal.SIGINT)
            try:
                p.wait(timeout=75)
            except subprocess.TimeoutExpired:
                print("WARN: aws-deploy.py did not finish its teardown in 75s; "
                      "killing it — check for a leaked AMI and snapshot",
                      flush=True)
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(p.pid), 9)

        if stray := self.live():        # a launch interrupted before it logged an id
            ec2().terminate_instances(InstanceIds=stray)

        text = log.read_text(errors="replace")
        row = parse(text, self.metrics)
        row["complete"] = bool(re.search(r"^COMPLETE:", text, re.M))
        row["log"] = log.name
        return row


if __name__ == "__main__":
    runner.cli(SmoltcpS3())
