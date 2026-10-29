"""Key-value store Lambda handler.

Routes (API Gateway REST, AWS_PROXY integration):
    GET    /kv          list keys (max 100)
    GET    /kv/{key}    read value
    PUT    /kv/{key}    body {"value": <any JSON>, "ttlSeconds": <int, optional>}
    DELETE /kv/{key}    delete key
"""
import json
import os
import time

import boto3

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
MAX_KEY_LEN = 256
MAX_VALUE_BYTES = 350_000  # DynamoDB item limit is 400 KB


def resp(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    method = event.get("httpMethod")
    key = (event.get("pathParameters") or {}).get("key")

    if key is None:
        if method != "GET":
            return resp(405, {"error": "method not allowed"})
        items = TABLE.scan(Limit=100, ProjectionExpression="pk").get("Items", [])
        return resp(200, {"keys": [i["pk"] for i in items]})

    if not key or len(key) > MAX_KEY_LEN:
        return resp(400, {"error": f"key must be 1-{MAX_KEY_LEN} chars"})

    if method == "GET":
        return get_key(key)
    if method == "PUT":
        return put_key(key, event.get("body"))
    if method == "DELETE":
        return delete_key(key)
    return resp(405, {"error": "method not allowed"})


def get_key(key):
    item = TABLE.get_item(Key={"pk": key}).get("Item")
    # DynamoDB TTL deletes lazily, so hide items that have already expired.
    if not item or ("expiresAt" in item and int(item["expiresAt"]) < time.time()):
        return resp(404, {"error": "not found"})
    return resp(200, {
        "key": key,
        "value": json.loads(item["value"]),
        "updatedAt": int(item["updatedAt"]),
    })


def put_key(key, raw_body):
    try:
        body = json.loads(raw_body or "{}")
    except json.JSONDecodeError:
        return resp(400, {"error": "body must be JSON"})
    if not isinstance(body, dict) or "value" not in body:
        return resp(400, {"error": 'body must be {"value": ...}'})

    value = json.dumps(body["value"])
    if len(value.encode()) > MAX_VALUE_BYTES:
        return resp(413, {"error": "value too large"})

    now = int(time.time())
    item = {"pk": key, "value": value, "updatedAt": now}
    ttl = body.get("ttlSeconds")
    if ttl is not None:
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            return resp(400, {"error": "ttlSeconds must be a positive integer"})
        item["expiresAt"] = now + ttl
    TABLE.put_item(Item=item)
    return resp(200, {"key": key, "updatedAt": now})


def delete_key(key):
    old = TABLE.delete_item(Key={"pk": key}, ReturnValues="ALL_OLD").get("Attributes")
    if not old:
        return resp(404, {"error": "not found"})
    return resp(200, {"deleted": key})
