#!/usr/bin/env python3
"""Add or remove one bucket-policy statement, keyed by Sid.

    bucket_policy.py add    --sid BenchBlobFromVpce --resource '.../blob.bin' --vpce vpce-…
    bucket_policy.py remove --sid BenchBinFromVpce

Both benches grant a read on the same bucket through the same gateway
endpoint, so each has to merge into what the other wrote. Writing the document
wholesale surfaced as a 404 on an instance that was already billing.

--bucket defaults to $AWS_BUCKET. An empty document is deleted, not put.
"""

from __future__ import annotations

import argparse
import json
import os

import boto3
from botocore.exceptions import ClientError

EMPTY = {"Version": "2012-10-17", "Statement": []}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("add", "remove"))
    ap.add_argument("--sid", required=True)
    ap.add_argument("--bucket", default=os.environ.get("AWS_BUCKET"))
    ap.add_argument("--resource", help="arn:aws:s3:::bucket/key; required to add")
    ap.add_argument("--vpce", help="gateway endpoint id; required to add")
    a = ap.parse_args()
    if not a.bucket:
        raise SystemExit("no bucket: pass --bucket or set AWS_BUCKET")
    if a.action == "add" and not (a.resource and a.vpce):
        raise SystemExit("add needs --resource and --vpce")

    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION"))
    try:
        doc = json.loads(s3.get_bucket_policy(Bucket=a.bucket)["Policy"])
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchBucketPolicy":
            raise
        doc = dict(EMPTY)

    kept = [s for s in doc.get("Statement", []) if s.get("Sid") != a.sid]
    if a.action == "add":
        # Read-only: the run log goes to the console, so nothing needs write.
        kept.append({
            "Sid": a.sid, "Effect": "Allow", "Principal": "*",
            "Action": "s3:GetObject", "Resource": a.resource,
            "Condition": {"StringEquals": {"aws:sourceVpce": a.vpce}},
        })
    doc["Statement"] = kept

    if kept:
        s3.put_bucket_policy(Bucket=a.bucket, Policy=json.dumps(doc))
        print(f"bucket policy: {a.action}ed {a.sid} ({len(kept)} statement(s))")
    else:
        s3.delete_bucket_policy(Bucket=a.bucket)
        print("bucket policy: removed (no statements left)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
