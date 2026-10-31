"""Consistent hashing ring Lambda handler (step 1: routing only, no data storage).

Routes (API Gateway REST, AWS_PROXY integration):
    POST   /servers              body {"serverId": "s0", "vnodes": 100}  add a server + its virtual nodes
    GET    /servers              list servers, vnode counts and share of the ring
    DELETE /servers/{serverId}   remove a server + its virtual nodes
    GET    /route/{key}[?n=3]    owning vnode/server; n = preference list of N distinct servers

Ring math (pure functions below) uses SHA-1, so positions are 0 .. 2^160 - 1.
A key belongs to the first vnode clockwise whose position is >= the key's position,
wrapping to the first vnode. A vnode owns the range (previous vnode, this vnode].
"""
import bisect
import hashlib
import json
import os
import re

import boto3
from boto3.dynamodb.conditions import Key

TABLE = boto3.resource("dynamodb").Table(os.environ.get("TABLE_NAME", "ring-local"))
RING_PK = "RING#default"
SERVER_PREFIX = "SERVER#"
VNODE_PREFIX = "VNODE#"

RING_SIZE = 2 ** 160
SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DEFAULT_VNODES = 100
MIN_VNODES, MAX_VNODES = 1, 500
MAX_KEY_LEN = 256
MAX_N = 10


# ---------- Pure ring functions (no AWS calls) ----------

def position(text):
    """SHA-1 position of a string on the ring, as an int in 0 .. 2^160 - 1."""
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest(), 16)


def to_hex(pos):
    return format(pos, "040x")


def vnodes_for(server_id, count):
    """Virtual nodes of one server: list of (position, serverId, vnodeIndex)."""
    return [(position(f"{server_id}#{i}"), server_id, i) for i in range(count)]


def build_ring(vnodes):
    """Sort vnodes by position. The ring is a sorted list of (position, serverId, vnodeIndex)."""
    return sorted(vnodes)


def lookup(ring, pos):
    """Index of the first vnode clockwise with position >= pos (wraps to 0)."""
    i = bisect.bisect_left(ring, (pos,))
    return i % len(ring)


def preference_list(ring, pos, n):
    """First n distinct physical servers clockwise from pos, skipping repeat servers."""
    start = lookup(ring, pos)
    chosen = []
    for step in range(len(ring)):
        _, server_id, _ = ring[(start + step) % len(ring)]
        if server_id not in chosen:
            chosen.append(server_id)
            if len(chosen) == n:
                break
    return chosen


def shares(ring):
    """Share of the ring (percent) owned by each server."""
    owned = {}
    for i, (pos, server_id, _) in enumerate(ring):
        prev = ring[i - 1][0]
        length = (pos - prev) % RING_SIZE or (RING_SIZE if len(ring) == 1 else 0)
        owned[server_id] = owned.get(server_id, 0) + length
    return {s: round(100 * v / RING_SIZE, 2) for s, v in owned.items()}


def changed_ranges(old_ring, new_ring):
    """Key ranges (start, end] whose owner differs between two rings.

    Every vnode position from both rings is a boundary. Within one segment between
    consecutive boundaries, each ring has a single owner: the vnode at the segment's end.
    Adjacent segments with the same (from, to) pair are merged.
    """
    points = sorted({v[0] for v in old_ring} | {v[0] for v in new_ring})
    out = []
    for i, end in enumerate(points):
        start = points[i - 1]
        old_owner = old_ring[lookup(old_ring, end)][1] if old_ring else None
        new_owner = new_ring[lookup(new_ring, end)][1] if new_ring else None
        if old_owner == new_owner:
            continue
        if out and out[-1]["end"] == start and (out[-1]["from"], out[-1]["to"]) == (old_owner, new_owner):
            out[-1]["end"] = end
        else:
            out.append({"start": start, "end": end, "from": old_owner, "to": new_owner})
    return [{"start": to_hex(r["start"]), "end": to_hex(r["end"]),
             "fromServer": r["from"], "toServer": r["to"]} for r in out]


# ---------- DynamoDB access ----------

def query_prefix(prefix):
    items, kwargs = [], {"KeyConditionExpression": Key("pk").eq(RING_PK) & Key("sk").begins_with(prefix)}
    while True:
        page = TABLE.query(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def load_ring():
    """Vnode items come back sorted by sk (zero-padded hex position), so the ring is already ordered."""
    return [(int(i["sk"][len(VNODE_PREFIX):], 16), i["serverId"], int(i["vnodeIndex"]))
            for i in query_prefix(VNODE_PREFIX)]


# ---------- HTTP handlers ----------

def resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def add_server(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return resp(400, {"error": "body must be JSON"})
    if not isinstance(body, dict):
        return resp(400, {"error": "body must be a JSON object"})
    server_id = body.get("serverId")
    vnodes = body.get("vnodes", DEFAULT_VNODES)
    if not isinstance(server_id, str) or not SERVER_ID_RE.match(server_id):
        return resp(400, {"error": "serverId must be 1-64 chars of A-Z a-z 0-9 _ . -"})
    if isinstance(vnodes, bool) or not isinstance(vnodes, int) or not MIN_VNODES <= vnodes <= MAX_VNODES:
        return resp(400, {"error": f"vnodes must be an integer {MIN_VNODES}-{MAX_VNODES}"})

    try:
        TABLE.put_item(
            Item={"pk": RING_PK, "sk": SERVER_PREFIX + server_id, "serverId": server_id, "vnodeCount": vnodes},
            ConditionExpression="attribute_not_exists(sk)",
        )
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        return resp(409, {"error": f"server {server_id} already exists"})

    old_ring = load_ring()
    new_vnodes = vnodes_for(server_id, vnodes)
    with TABLE.batch_writer() as batch:
        for pos, sid, idx in new_vnodes:
            batch.put_item(Item={"pk": RING_PK, "sk": VNODE_PREFIX + to_hex(pos), "serverId": sid, "vnodeIndex": idx})
    new_ring = build_ring(old_ring + new_vnodes)
    ranges = changed_ranges(old_ring, new_ring)
    return resp(201, {"serverId": server_id, "vnodesAdded": len(new_vnodes),
                      "affectedRanges": len(ranges), "ranges": ranges})


def remove_server(server_id):
    if not SERVER_ID_RE.match(server_id or ""):
        return resp(400, {"error": "invalid serverId"})
    if "Item" not in TABLE.get_item(Key={"pk": RING_PK, "sk": SERVER_PREFIX + server_id}):
        return resp(404, {"error": f"server {server_id} not found"})

    old_ring = load_ring()
    new_ring = [v for v in old_ring if v[1] != server_id]
    with TABLE.batch_writer() as batch:
        for pos, sid, _ in old_ring:
            if sid == server_id:
                batch.delete_item(Key={"pk": RING_PK, "sk": VNODE_PREFIX + to_hex(pos)})
    TABLE.delete_item(Key={"pk": RING_PK, "sk": SERVER_PREFIX + server_id})
    ranges = changed_ranges(old_ring, new_ring)
    return resp(200, {"serverId": server_id, "vnodesRemoved": len(old_ring) - len(new_ring),
                      "affectedRanges": len(ranges), "ranges": ranges})


def list_servers():
    servers = query_prefix(SERVER_PREFIX)
    pct = shares(load_ring())
    return resp(200, {"servers": [{"serverId": s["serverId"], "vnodes": int(s["vnodeCount"]),
                                   "sharePercent": pct.get(s["serverId"], 0.0)} for s in servers]})


def route(key, query):
    if not key or len(key.encode("utf-8")) > MAX_KEY_LEN:
        return resp(400, {"error": f"key must be 1-{MAX_KEY_LEN} bytes"})
    n = None
    if "n" in query:
        if not query["n"].isdigit() or not 1 <= int(query["n"]) <= MAX_N:
            return resp(400, {"error": f"n must be an integer 1-{MAX_N}"})
        n = int(query["n"])
    ring = load_ring()
    if not ring:
        return resp(409, {"error": "ring is empty; add a server first"})
    pos = position(key)
    vpos, server_id, idx = ring[lookup(ring, pos)]
    body = {"key": key, "position": to_hex(pos),
            "vnode": {"position": to_hex(vpos), "serverId": server_id, "vnodeIndex": idx},
            "serverId": server_id}
    if n:
        body["preferenceList"] = preference_list(ring, pos, n)
    return resp(200, body)


def handler(event, context):
    method = event.get("httpMethod")
    resource = event.get("resource")
    params = event.get("pathParameters") or {}
    if resource == "/servers" and method == "POST":
        return add_server(event)
    if resource == "/servers" and method == "GET":
        return list_servers()
    if resource == "/servers/{serverId}" and method == "DELETE":
        return remove_server(params.get("serverId"))
    if resource == "/route/{key}" and method == "GET":
        return route(params.get("key"), event.get("queryStringParameters") or {})
    return resp(405, {"error": "method not allowed"})
