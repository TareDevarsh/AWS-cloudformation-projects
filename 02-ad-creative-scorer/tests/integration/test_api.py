#!/usr/bin/env python3
"""End-to-end test for the Ad Creative Scorer API using SigV4-signed requests.

Usage: python3 tests/integration/test_api.py [env] [region]
Needs: boto3, credentials allowed to execute-api:Invoke (attach the
ClientPolicyArn output of the api stack), and all six stacks deployed
(the SageMaker endpoint must exist for a creative to reach SCORED).
"""
import json
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

ENV = sys.argv[1] if len(sys.argv) > 1 else "dev"
SESSION = boto3.Session(region_name=sys.argv[2] if len(sys.argv) > 2 else None)
REGION = SESSION.region_name or "us-east-1"
PROJECT = "ad-scorer"
POLL_SECONDS = 90


def stack_output(suffix, key):
    cfn = SESSION.client("cloudformation", region_name=REGION)
    stack = cfn.describe_stacks(StackName=f"{PROJECT}-{ENV}-{suffix}")["Stacks"][0]
    return next(o["OutputValue"] for o in stack["Outputs"] if o["OutputKey"] == key)


def _decode(raw):
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {"raw": raw.decode("utf-8", "replace")}


def send(method, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _decode(e.read())


def call(method, url, body=None):
    """SigV4-signed API call."""
    data = json.dumps(body).encode() if body is not None else None
    req = AWSRequest(method=method, url=url, data=data,
                     headers={"Content-Type": "application/json"} if data else {})
    SigV4Auth(SESSION.get_credentials(), "execute-api", REGION).add_auth(req)
    return send(method, url, data, dict(req.headers))


def png(width=128, height=128):
    """Small solid-colour PNG built with the standard library."""
    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))
    row = b"\x00" + b"\x30\x90\xe0" * width
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height))
            + chunk(b"IEND", b""))


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}{': ' + str(detail) if detail else ''}")
    return ok


def main():
    base = stack_output("api", "APIEndpoint")
    bucket = stack_output("foundation", "CreativeAssetsBucketName")
    print(f"Endpoint: {base}\n")
    results = []

    # Unsigned requests must be rejected by the IAM gate.
    s, _ = send("POST", f"{base}/v1/upload", b'{"content_type":"image/png","campaign_id":"it"}',
                {"Content-Type": "application/json"})
    results.append(check("unsigned POST /v1/upload -> 403", s == 403, f"status {s}"))
    s, _ = send("GET", f"{base}/v1/score/unsigned-check")
    results.append(check("unsigned GET /v1/score/{id} -> 403", s == 403, f"status {s}"))

    # Input validation.
    s, _ = call("POST", f"{base}/v1/upload", {"content_type": "image/png", "campaign_id": "../evil"})
    results.append(check("bad campaign_id -> 400", s == 400, f"status {s}"))

    # Upload flow.
    s, up = call("POST", f"{base}/v1/upload", {"content_type": "image/png", "campaign_id": "integration-test"})
    ok = check("POST /v1/upload -> 200", s == 200 and "upload_url" in up, f"status {s}")
    results.append(ok)
    if not ok:
        return finish(results)
    creative_id, s3_key = up["creative_id"], up["s3_key"]

    s, _ = send("PUT", up["upload_url"], png(), {"Content-Type": "image/png"})
    results.append(check("PUT creative to presigned URL -> 200", s == 200, f"status {s}"))

    s, _ = call("POST", f"{base}/v1/score",
                {"s3_bucket": "someone-elses-bucket", "s3_key": s3_key, "creative_id": creative_id})
    results.append(check("POST /v1/score foreign bucket -> 400", s == 400, f"status {s}"))

    s, sc = call("POST", f"{base}/v1/score",
                 {"s3_bucket": bucket, "s3_key": s3_key, "creative_id": creative_id,
                  "campaign_id": "integration-test"})
    results.append(check("POST /v1/score -> 202", s == 202 and sc.get("status") == "PENDING", f"status {s}"))

    # Poll until the pipeline finishes.
    status, deadline = None, time.time() + POLL_SECONDS
    while time.time() < deadline:
        s, item = call("GET", f"{base}/v1/score/{creative_id}")
        status = item.get("status") if s == 200 else None
        if status in ("SCORED", "FLAGGED", "FAILED"):
            break
        time.sleep(5)
    results.append(check("GET /v1/score/{id} reaches SCORED or FLAGGED",
                         status in ("SCORED", "FLAGGED"), f"final status {status}"))

    s, _ = call("GET", f"{base}/v1/score/does-not-exist-0000")
    results.append(check("GET unknown creative -> 404", s == 404, f"status {s}"))

    return finish(results)


def finish(results):
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
