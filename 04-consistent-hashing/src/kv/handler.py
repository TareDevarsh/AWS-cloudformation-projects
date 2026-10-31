"""Quorum key-value coordinator (step 3: vector clocks, conflict detection, server-side LWW).

Routes (API Gateway REST, AWS_PROXY integration):
    PUT /kv/{key}[?w=2][&coordinator=s0]   body {"value": "...", "context": "<opaque>"}
    GET /kv/{key}[?r=2]

Each key is stored on the N preference-list replicas (first N distinct servers clockwise from
SHA-1(key)). Strict quorum: a server marked down counts as a failed replica (no sloppy quorum,
no hinted handoff).

Versioning uses a vector clock (vector_clock.py) instead of step 2's integer counter:
- The coordinator is the first up server in the preference list, or ?coordinator=<serverId> if it
  is in the preference list and up. On a write it increments its own clock entry.
- GET returns the winning value plus an opaque `context` (the merged clock of every version seen),
  so the next PUT carrying that context supersedes all of them.
- PUT builds the new clock from the request context (empty if absent) with the coordinator's entry
  incremented. A PUT without context can therefore be concurrent with existing data.
- Each replica item stores a list of sibling versions ({value, clock, timestamp}). On write it
  drops stored versions that are ancestors of the new clock, keeps concurrent ones, and ignores
  the write if a stored version already descends from it. A DynamoDB conditional write on a rev
  attribute with bounded retry keeps concurrent writers to one item from clobbering each other.
- Read collects R responses, unions versions, drops dominated ones. More than one concurrent
  version left is a conflict: the coordinator resolves it server-side by last-write-wins (highest
  timestamp, tiebreak by highest coordinator serverId). LWW can silently drop a concurrent write
  and depends on server clocks being close; the resolution is NOT written back (no read repair),
  so the next PUT with the returned context supersedes the siblings.
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3

import vector_clock as vc
from ring_math import position, preference_list, to_hex

DDB = boto3.client("dynamodb")  # low-level client: thread-safe for the parallel replica calls
RING_TABLE = os.environ.get("RING_TABLE_NAME", "ring-local")
DATA_TABLE = os.environ.get("DATA_TABLE_NAME", "data-local")
RING_PK = "RING#default"
N = int(os.environ.get("QUORUM_N", "3"))
W = int(os.environ.get("QUORUM_W", "2"))
R = int(os.environ.get("QUORUM_R", "2"))
MAX_CLOCK_ENTRIES = int(os.environ.get("MAX_CLOCK_ENTRIES", "10"))
MAX_SIBLINGS = 10            # cap the sibling list per replica item
REPLICA_WRITE_RETRIES = 5    # bounded retry on the conditional write when a concurrent write wins

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
    """Stored versions for one replica: {"rev": int, "versions": [{value, clock, timestamp}]}.

    None if the item does not exist. `rev` backs the optimistic-concurrency conditional write.
    """
    item = DDB.get_item(TableName=DATA_TABLE, ConsistentRead=True,
                        Key={"serverId": {"S": server_id}, "itemKey": {"S": key}}).get("Item")
    if not item:
        return None
    return {"rev": int(item["rev"]["N"]), "versions": json.loads(item["versions"]["S"])}


def _put_replica_item(server_id, key, versions, new_rev, prev_rev):
    """Conditional PutItem: only if the stored rev still equals prev_rev (None = item absent)."""
    kwargs = {
        "TableName": DATA_TABLE,
        "Item": {"serverId": {"S": server_id}, "itemKey": {"S": key},
                 "rev": {"N": str(new_rev)}, "versions": {"S": json.dumps(versions)}},
    }
    if prev_rev is None:
        kwargs["ConditionExpression"] = "attribute_not_exists(itemKey)"
    else:
        kwargs["ConditionExpression"] = "rev = :r"
        kwargs["ExpressionAttributeValues"] = {":r": {"N": str(prev_rev)}}
    DDB.put_item(**kwargs)


# ---------- Version-set reconciliation (pure) ----------

def reconcile(versions):
    """Drop versions dominated by another version in the set. Returns the surviving siblings.

    A version survives unless some other version strictly descends from it (a newer write).
    Equal clocks are de-duplicated, keeping the first.
    """
    survivors = []
    for i, v in enumerate(versions):
        dominated = False
        for j, other in enumerate(versions):
            if i == j:
                continue
            rel = vc.compare(v["clock"], other["clock"])
            if rel == vc.ANCESTOR:            # other is strictly newer than v
                dominated = True
                break
            if rel == vc.EQUAL and j < i:     # keep only the first of equal clocks
                dominated = True
                break
        if not dominated:
            survivors.append(v)
    return survivors


def apply_write(stored_versions, new_version):
    """Merge a new version into a replica's stored versions.

    Returns (versions, changed). If a stored version already descends from the new one, the write
    is a no-op (changed=False). Otherwise drop stored versions the new one supersedes, keep
    concurrent ones, add the new version, and cap the sibling list.
    """
    for v in stored_versions:
        if vc.descends(v["clock"], new_version["clock"]):
            return stored_versions, False        # a stored version is newer or equal: ignore
    kept = [v for v in stored_versions if not vc.descends(new_version["clock"], v["clock"])]
    merged = kept + [new_version]
    merged = reconcile(merged)
    if len(merged) > MAX_SIBLINGS:               # cap siblings; drop the oldest by timestamp
        merged = sorted(merged, key=lambda v: (v["timestamp"], vc.encode_context(v["clock"])),
                        reverse=True)[:MAX_SIBLINGS]
    return merged, True


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


def pick_coordinator(query, pref, up):
    """Chosen coordinator serverId, or a (code, body) error tuple.

    Default is the first up server in the preference list. ?coordinator= overrides it but must be
    in the preference list (else 400) and up (else 503).
    """
    requested = query.get("coordinator")
    if requested is None:
        return up[0]
    if requested not in pref:
        return 400, {"error": f"coordinator {requested} is not in the preference list {pref}"}
    if requested not in up:
        return 503, {"error": f"coordinator {requested} is down"}
    return requested


def write_replica(server_id, key, new_version):
    """Apply new_version to one replica with a bounded optimistic-concurrency retry.

    Re-reads and retries when a concurrent write bumped the rev. Returns the write outcome:
    "written", "nochange" (a stored version already dominates), or raises on repeated conflicts.
    """
    for _ in range(REPLICA_WRITE_RETRIES):
        current = get_replica(server_id, key)
        stored = current["versions"] if current else []
        prev_rev = current["rev"] if current else None
        merged, changed = apply_write(stored, new_version)
        if not changed:
            return "nochange"
        try:
            _put_replica_item(server_id, key, merged, (prev_rev or 0) + 1, prev_rev)
            return "written"
        except DDB.exceptions.ConditionalCheckFailedException:
            continue  # someone else wrote this item first; re-read and retry
    raise RuntimeError("replica write contended out")


def version_views(versions):
    """Client-facing view of a list of stored versions: value, clock, timestamp, coordinator."""
    return [{"value": v["value"], "clock": v["clock"], "timestamp": v["timestamp"],
             "coordinator": v.get("coordinator")} for v in versions]


def put_key(key, event, query):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return resp(400, {"error": "body must be JSON"})
    value = body.get("value") if isinstance(body, dict) else None
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        return resp(400, {"error": 'body must be {"value": string} with value up to %d bytes' % MAX_VALUE_BYTES})
    try:
        context_clock = vc.decode_context(body.get("context") if isinstance(body, dict) else None)
    except ValueError as e:
        return resp(400, {"error": str(e)})
    try:
        w = quorum_param(query, "w", W)
    except ValueError as e:
        return resp(400, {"error": str(e)})

    pref, up = placement(key)
    if not pref:
        return resp(409, {"error": "ring is empty; add a server first"})
    coord = pick_coordinator(query, pref, up)
    if isinstance(coord, tuple):
        code, cbody = coord
        return resp(code, {"key": key, "preferenceList": pref, **cbody})

    info = {"key": key, "position": to_hex(position(key)), "preferenceList": pref,
            "coordinator": coord, "n": len(pref), "nConfigured": N, "w": w}
    failed = [{"serverId": s, "reason": "down"} for s in pref if s not in up]
    if len(up) < w:
        return resp(503, {**info, "error": f"only {len(up)} of {len(pref)} replicas up, need w={w}; write not attempted",
                          "acked": [], "failed": failed})

    # New clock = context clock with the coordinator's own entry incremented, then truncated.
    now = datetime.now(timezone.utc).isoformat()
    new_clock = vc.truncate(vc.increment(context_clock, coord, now), MAX_CLOCK_ENTRIES)
    new_version = {"value": value, "clock": new_clock, "timestamp": now, "coordinator": coord}

    writes = fan_out(lambda s: write_replica(s, key, new_version), up)
    acked = [s for s in up if writes[s][1] is None]
    failed += [{"serverId": s, "reason": writes[s][1]} for s in up if writes[s][1]]
    out = {**info, "clock": new_clock, "context": vc.encode_context(new_clock),
           "updatedAt": now, "acked": acked, "failed": failed}
    if len(acked) < w:
        return resp(503, {**out, "error": f"{len(acked)} acks, need w={w}"})
    return resp(200, out)


def resolve_lww(versions):
    """Pick one winner from concurrent versions: highest timestamp, tiebreak highest coordinator id.

    Each version records the serverId that coordinated its write, so the tiebreak is deterministic
    even when two writers share a timestamp.
    """
    return max(versions, key=lambda v: (v["timestamp"], v.get("coordinator") or ""))


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
    replicas, counted, collected = [], 0, []
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
        versions = rec["versions"] if rec else []
        if use:
            collected.extend(versions)
        replicas.append({"serverId": s, "status": "ok", "found": rec is not None,
                         "versions": version_views(versions), "counted": bool(use)})
    if counted < r:
        return resp(503, {**info, "error": f"{counted} replicas responded, need r={r}", "replicas": replicas})

    survivors = reconcile(collected)
    if not survivors:
        return resp(404, {**info, "error": f"key {key} not found", "replicas": replicas})

    merged_clock = vc.merge(*(v["clock"] for v in survivors))
    context = vc.encode_context(merged_clock)
    detected = len(survivors) > 1
    winner = resolve_lww(survivors) if detected else survivors[0]
    discarded = [version_views([v])[0] for v in survivors if v is not winner] if detected else []
    conflict = {"detected": detected, "versions": version_views(survivors),
                "winner": version_views([winner])[0], "discarded": discarded}
    return resp(200, {**info, "value": winner["value"], "clock": winner["clock"], "context": context,
                      "conflict": conflict, "replicas": replicas})


def handler(event, context):
    method = event.get("httpMethod")
    key = (event.get("pathParameters") or {}).get("key") or ""
    query = event.get("queryStringParameters") or {}
    if event.get("resource") != "/kv/{key}" or method not in ("PUT", "GET"):
        return resp(405, {"error": "method not allowed"})
    if not KEY_RE.match(key):
        return resp(400, {"error": "key must be 1-256 chars of A-Z a-z 0-9 _ . : -"})
    return put_key(key, event, query) if method == "PUT" else get_key(key, query)
