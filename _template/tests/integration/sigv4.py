"""Shared helpers for integration tests: stack outputs + SigV4-signed HTTP calls.

    from sigv4 import stack_output, call, unsigned_status
    base = stack_output("{{slug}}-dev", "ApiEndpoint")
    assert unsigned_status(f"{base}/path") == 403
    status, body = call("GET", f"{base}/path")
"""
import json
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

SESSION = boto3.Session()
REGION = SESSION.region_name or "us-east-1"


def stack_output(stack, key, region=REGION):
    cfn = SESSION.client("cloudformation", region_name=region)
    outputs = cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])
    return next(o["OutputValue"] for o in outputs if o["OutputKey"] == key)


def call(method, url, body=None, region=REGION, service="execute-api"):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = AWSRequest(method=method, url=url, data=data, headers=headers)
    SigV4Auth(SESSION.get_credentials(), service, region).add_auth(req)
    r = urllib.request.Request(url, data=data, method=method, headers=dict(req.headers))
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def unsigned_status(url):
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
