"""Quorum key-value coordinator (step 2: N replicas, W write quorum, R read quorum).

Routes (API Gateway REST, AWS_PROXY integration):
    PUT /kv/{key}[?w=2]   body {"value": "..."}  write to the N preference-list replicas
    GET /kv/{key}[?r=2]   read the replicas, return the highest version among the first R responses

Each replica is one item in the data table (serverId, itemKey). The preference list is
the first N distinct physical servers clockwise from SHA-1(key) (ring_math.preference_list).
Strict quorum: a server marked down in the ring table counts as a failed replica; there is
no sloppy quorum or hinted handoff. Versioning is a simple integer counter per key
(highest version seen on reachable replicas + 1), so concurrent writers are last-writer-wins.
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3

from ring_math import position, preference_list, to_hex

DDB = boto3.client("dynamodb")  # low-level client: thread-safe for the parallel replica calls
RING_TABLE = os.environ.get("RING_TABLE_NAME", "ring-local")
DATA_TABLE = os.environ.get("DATA_TABLE_NAME", "data-local")
RING_PK = "RING#default"
N = int(os.environ.get("QUORUM_N", "3"))
W = int(os.environ.get("QUORUM_W", "2"))
R = int(os.environ.get("QUORUM_R", "2"))

KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
MAX_VALUE_BYTES = 10 * 1024


# ---------- Storage (one function per DynamoDB call, so they are easy to fake locally) ----------

def load_cluster():
    """Sorted ring of (position, serverId, vnodeIndex) and {serverId: "up"|"down"} from one partition."""
    ring, status = [], {}
    kwargs = {"TableName": RING_TABLE, "KeyConditionExpression": "pk = :pk",
              "ExpressionAttributeValues": {":pk": {"S": RING_PK}}}
    while True:
        page = DDB.query(**kwargs)
        for item in page.get("Items", []):
            sk, sid = item["sk"]["S"], item["serverId"]["S"]
            if sk.startswith("VNODE#"):
                ring.append((int(sk[len("VNODE#"):], 16), sid, int(item["vnodeIndex"]["N"])))
            elif sk.startswith("SERVER#"):
                status[sid] = item.get("status", {}).get("S", "up")
        if "LastEvaluatedKey" not in page:
            return sorted(ring), status
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def get_replica(server_id, key):
    item = DDB.get_item(TableName=DATA_TABLE, ConsistentRead=True,
                        Key={"serverId": {"S": server_id}, "itemKey": {"S": key}}).get("Item")
    if not item:
        return None
    return {"value": item["value"]["S"], "version": int(item["version"]["N"]), "updatedAt": item["updatedAt"]["S"]}


def put_replica(server_id, key, record):
    DDB.put_item(TableName=DATA_TABLE, Item={
        "serverId": {"S": server_id}, "itemKey": {"S": key}, "value": {"S": record["value"]},
        "version": {"N": str(record["version"])}, "updatedAt": {"S": record["updatedAt"]}})


# ---------- Coordinator ----------

def resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def fan_out(fn, servers):
    """Call fn(server) for every server in parallel. Returns {server: (result, error)}."""
    if not servers:
        return {}
    with ThreadPoolExecutor(max_workers=len(servers)) as pool:
        futures = {s: pool.submit(fn, s) for s in servers}
    out = {}
    for s, f in futures.items():
        try:
            out[s] = (f.result(), None)
        except Exception as e:  # a failed replica call is a failed response, not a crash
            out[s] = (None, type(e).__name__)
    return out


def quorum_param(query, name, default):
    raw = query.get(name)
    if raw is None:
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= N:
        raise ValueError(f"{name} must be an integer 1-{N}")
    return int(raw)


def placement(key):
    """Preference list for key (first N distinct servers) and which of them are up."""
    ring, status = load_cluster()
    pref = preference_list(ring, position(key), N) if ring else []
    up = [s for s in pref if status.get(s, "up") == "up"]
    return pref, up


def put_key(key, event, query):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return resp(400, {"error": "body must be JSON"})
    value = body.get("value") if isinstance(body, dict) else None
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        return resp(400, {"error": 'body must be {"value": string} with value up to %d bytes' % MAX_VALUE_BYTES})
    try:
        w = quorum_param(query, "w", W)
    except ValueError as e:
        return resp(400, {"error": str(e)})

    pref, up = placement(key)
    if not pref:
        return resp(409, {"error": "ring is empty; add a server first"})
    info = {"key": key, "position": to_hex(position(key)), "preferenceList": pref,
            "n": len(pref), "nConfigured": N, "w": w}
    failed = [{"serverId": s, "reason": "down"} for s in pref if s not in up]
    if len(up) < w:
        return resp(503, {**info, "error": f"only {len(up)} of {len(pref)} replicas up, need w={w}; write not attempted",
                          "acked": [], "failed": failed})

    # Read-before-write on the reachable replicas to pick the next version.
    reads = fan_out(lambda s: get_replica(s, key), up)
    reachable = [s for s in up if reads[s][1] is None]
    failed += [{"serverId": s, "reason": reads[s][1]} for s in up if reads[s][1]]
    version = max((reads[s][0]["version"] for s in reachable if reads[s][0]), default=0) + 1
    record = {"value": value, "version": version, "updatedAt": datetime.now(timezone.utc).isoformat()}

    writes = fan_out(lambda s: put_replica(s, key, record), reachable)
    acked = [s for s in reachable if writes[s][1] is None]
    failed += [{"serverId": s, "reason": writes[s][1]} for s in reachable if writes[s][1]]
    out = {**info, "version": version, "updatedAt": record["updatedAt"], "acked": acked, "failed": failed}
    if len(acked) < w:
        return resp(503, {**out, "error": f"{len(acked)} acks, need w={w}"})
    return resp(200, out)


def get_key(key, query):
    try:
        r = quorum_param(query, "r", R)
    except ValueError as e:
        return resp(400, {"error": str(e)})

    pref, up = placement(key)
    if not pref:
        return resp(409, {"error": "ring is empty; add a server first"})
    info = {"key": key, "preferenceList": pref, "n": len(pref), "nConfigured": N, "r": r}
    reads = fan_out(lambda s: get_replica(s, key), up)

    # Count the first r successful responses in preference-list order; report every replica.
    replicas, counted = [], 0
    for s in pref:
        if s not in up:
            replicas.append({"serverId": s, "status": "down", "counted": False})
            continue
        rec, err = reads[s]
        if err:
            replicas.append({"serverId": s, "status": "error", "error": err, "counted": False})
            continue
        use = counted < r
        counted += use
        replicas.append({"serverId": s, "status": "ok", "found": rec is not None, **(rec or {}), "counted": use})
    if counted < r:
        return resp(503, {**info, "error": f"{counted} replicas responded, need r={r}", "replicas": replicas})

    best = max((x for x in replicas if x["counted"] and x["found"]), key=lambda x: x["version"], default=None)
    if not best:
        return resp(404, {**info, "error": f"key {key} not found", "replicas": replicas})
    stale = [x["serverId"] for x in replicas
             if x["status"] == "ok" and (not x["found"] or x["version"] < best["version"])]
    return resp(200, {**info, "value": best["value"], "version": best["version"], "updatedAt": best["updatedAt"],
                      "staleReplicas": stale, "replicas": replicas})


def handler(event, context):
    method = event.get("httpMethod")
    key = (event.get("pathParameters") or {}).get("key") or ""
    query = event.get("queryStringParameters") or {}
    if event.get("resource") != "/kv/{key}" or method not in ("PUT", "GET"):
        return resp(405, {"error": "method not allowed"})
    if not KEY_RE.match(key):
        return resp(400, {"error": "key must be 1-256 chars of A-Z a-z 0-9 _ . : -"})
    return put_key(key, event, query) if method == "PUT" else get_key(key, query)
