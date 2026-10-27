#!/usr/bin/env python3
"""End-to-end smoke test for the KV API using SigV4-signed requests.

Usage: python3 scripts/test_kv.py [stage] [region]
Needs: boto3, credentials allowed to execute-api:Invoke (see ClientPolicyArn output).
"""
import json
import sys
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

STAGE = sys.argv[1] if len(sys.argv) > 1 else "dev"
SESSION = boto3.Session(region_name=sys.argv[2] if len(sys.argv) > 2 else None)
REGION = SESSION.region_name or "us-east-1"


def endpoint():
    cfn = SESSION.client("cloudformation", region_name=REGION)
    outputs = cfn.describe_stacks(StackName=f"kv-store-{STAGE}")["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in outputs if o["OutputKey"] == "ApiEndpoint")


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = AWSRequest(method=method, url=url, data=data,
                     headers={"Content-Type": "application/json"} if data else {})
    SigV4Auth(SESSION.get_credentials(), "execute-api", REGION).add_auth(req)
    r = urllib.request.Request(url, data=data, method=method, headers=dict(req.headers))
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: status {got}")
    return ok


def main():
    base = endpoint()
    print(f"Endpoint: {base}\n")
    results = []

    # Unsigned request must be rejected by the IAM gate.
    try:
        urllib.request.urlopen(f"{base}/kv")
        results.append(check("unsigned request denied", 200, 403))
    except urllib.error.HTTPError as e:
        results.append(check("unsigned request denied", e.code, 403))

    s, b = call("PUT", f"{base}/kv/greeting", {"value": {"hello": "world"}})
    results.append(check("PUT greeting", s, 200))
    s, b = call("GET", f"{base}/kv/greeting")
    results.append(check("GET greeting", s, 200) and b.get("value") == {"hello": "world"})
    s, b = call("GET", f"{base}/kv")
    results.append(check("LIST keys", s, 200) and "greeting" in b.get("keys", []))
    s, _ = call("DELETE", f"{base}/kv/greeting")
    results.append(check("DELETE greeting", s, 200))
    s, _ = call("GET", f"{base}/kv/greeting")
    results.append(check("GET after delete -> 404", s, 404))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
