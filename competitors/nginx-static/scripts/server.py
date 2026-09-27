#!/usr/bin/env python3
"""EC2 user-data for the static file server the HTTP load benches dial.

Fetches the static nginx from the bucket, lays a blob of the bench's object
size in tmpfs so no disk sits behind the server, serves it on port 80, and
prints `SERVER READY ip=<private ip>` on the console for the driver
(scripts/bench/httpserver.py) to read. The driver terminates the instance; a
timed shutdown backstops a dead driver. Stdlib only.

    BENCH_LOCAL=1 BENCH_WORK=/tmp/w BENCH_WWW=/tmp/www AWS_BUCKET=... AWS_REGION=... \\
    python3 scripts/server.py
"""

import os
import shutil
import subprocess
import time
import urllib.request

try:
    CONFIG  # type: ignore[used-before-def]  # noqa: B018
except NameError:
    CONFIG = {}


def cfg(key, default=""):
    v = CONFIG.get(key, os.environ.get(key, default))
    return "" if v is None else str(v)


LOCAL = cfg("BENCH_LOCAL", "0") == "1"
WORK = cfg("BENCH_WORK", "/run/nginx")
WWW = cfg("BENCH_WWW", "/dev/shm/www")
SIZE = cfg("BENCH_OBJECT_SIZE", "10G")
WORKERS = cfg("BENCH_SERVER_WORKERS", "auto") or "auto"
ENDPOINT = "https://{}.s3.{}.amazonaws.com".format(cfg("AWS_BUCKET"), cfg("AWS_REGION"))
BIN_PREFIX = cfg("BENCH_BIN_PREFIX", "bin/nginx-static")

# Stock AL2023 otherwise: nothing tuned, so the server is the same for both arms.
CONF = """\
daemon off;
# The nix build defaults to group "nogroup", which AL2023 does not have.
user nobody nobody;
worker_processes {workers};
worker_rlimit_nofile 200000;
error_log {work}/error.log warn;
pid {work}/nginx.pid;
events {{ worker_connections 65536; multi_accept on; }}
http {{
    access_log off;
    default_type application/octet-stream;
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 75;
    keepalive_requests 1000000;
    server {{
        listen 80 default_server reuseport backlog=65535;
        root {www};
    }}
}}
"""


def say(text=""):
    print(text, flush=True)  # cloud-init puts user-data output on the console


def imds(path):
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"})
        token = urllib.request.urlopen(req, timeout=3).read().decode()
        req = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/" + path,
            headers={"X-aws-ec2-metadata-token": token})
        return urllib.request.urlopen(req, timeout=3).read().decode()
    except Exception:
        return ""


def main():
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(WWW, exist_ok=True)
    nginx = os.path.join(WORK, "nginx")
    url = ENDPOINT + "/" + BIN_PREFIX + "/nginx"
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(nginx, "wb") as fh:
            shutil.copyfileobj(r, fh)
    except Exception as e:
        say("SERVER FAILED: fetch {}: {}".format(url, e))
        return 1
    os.chmod(nginx, 0o755)
    # fallocate on tmpfs is memory, not disk: the server never touches a disk.
    blob = os.path.join(WWW, "blob.bin")
    if subprocess.run(["fallocate", "-l", SIZE, blob]).returncode:
        say("SERVER FAILED: fallocate {} {}".format(SIZE, blob))
        return 1
    conf = os.path.join(WORK, "nginx.conf")
    with open(conf, "w") as fh:
        fh.write(CONF.format(workers=WORKERS, work=WORK, www=WWW))
    p = subprocess.Popen([nginx, "-p", WORK, "-c", conf])
    # Ready when a ranged GET answers 206.
    for _ in range(60):
        time.sleep(0.5)
        if p.poll() is not None:
            say("SERVER FAILED: nginx exited {}".format(p.returncode))
            return 1
        try:
            req = urllib.request.Request("http://127.0.0.1/blob.bin", headers={"Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status == 206:
                    break
        except Exception:
            pass
    else:
        say("SERVER FAILED: no 206 from nginx")
        return 1
    say("SERVER READY ip={} object={} workers={} nproc={} type={} kernel={}".format(
        imds("local-ipv4") or "unknown", SIZE, WORKERS, os.cpu_count(),
        imds("instance-type") or "unknown", os.uname().release))
    if not LOCAL:
        subprocess.run(["shutdown", "-h", "+120"])  # the driver terminates us first
        p.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
