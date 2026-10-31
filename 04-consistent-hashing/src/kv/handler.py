"""Quorum key-value coordinator (step 4: sloppy quorum + hinted handoff).

Routes (API Gateway REST, AWS_PROXY integration):
    PUT  /kv/{key}[?w=2][&coordinator=s0]   body {"value": "...", "context": "<opaque>"}
    GET  /kv/{key}[?r=2]
    POST /servers/{serverId}/up             mark a server up, then replay hints destined for it
    POST /hints/replay                      body {"serverId": "s0"} (optional); replay replayable hints

Each key's primary replicas are the first N distinct physical servers clockwise from SHA-1(key)
(the preference list, unchanged from step 3). Step 4 makes the quorum **sloppy**: when a primary
is down (status != up) the coordinator keeps walking the ring to the next healthy distinct physical
server not already chosen and uses it as a STAND-IN for that primary.

- Sloppy write: for each of the N primaries, write to it if up, else to its stand-in. A stand-in
  write stores the replica item PLUS a hint: the attribute `hintFor` (a JSON list of intended-but-down
  primary serverIds) on that stand-in's replica item. The item is otherwise a normal replica item
  (same serverId/itemKey/rev/versions), so `hintFor` is what distinguishes a hinted stand-in copy.
  The hinted value keeps its vector clock, so replay merges cleanly. 503 if fewer than W acks even
  after walking (not enough healthy servers).
- Sloppy read: a GET walks past down primaries to the same stand-ins, so it can read a value that
  currently lives only on a stand-in during the outage window a strict read would miss. It unions
  versions across primaries AND stand-ins, drops dominated versions, and resolves a remaining
  conflict by LWW exactly as step 3. Per-replica details flag each responder as a primary or a
  stand-in (and which intended server it stands in for). No read repair is performed.
- Hint replay (option A): `POST /servers/{serverId}/up` sets the server up and replays hints
  destined for it: every stand-in item whose `hintFor` lists serverId is written back to the now-up
  primary (a normal vector-clock merge write to just that server); on success serverId is removed
  from that item's `hintFor`, and the attribute is dropped when the list is empty. `POST /hints/replay`
  does the same on demand (optional {serverId}; without it, replay every hint whose intended server
  is now up). Replay is idempotent — the vector-clock merge plus conditional write prevent clobbering,
  so running it twice is safe. Work is bounded per call (HINT_SCAN_LIMIT items, HINT_REPLAY_LIMIT
  replays) and the response reports whether more remain.

Versioning (vector clocks, conflict detection, server-side LWW) is unchanged from step 3.
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
HINT_SCAN_LIMIT = int(os.environ.get("HINT_SCAN_LIMIT", "500"))    # max items scanned per replay call
HINT_REPLAY_LIMIT = int(os.environ.get("HINT_REPLAY_LIMIT", "100"))  # max hints handed off per call

KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
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


def set_server_status(server_id, status):
    """Mark a server up/down on the ring table. Returns True if the server exists, else False."""
    try:
        DDB.update_item(
            TableName=RING_TABLE,
            Key={"pk": {"S": RING_PK}, "sk": {"S": "SERVER#" + server_id}},
            UpdateExpression="SET #st = :st",
            ConditionExpression="attribute_exists(sk)",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":st": {"S": status}},
        )
        return True
    except DDB.exceptions.ConditionalCheckFailedException:
        return False


def get_replica(server_id, key):
    """Stored state for one replica: {"rev": int, "versions": [...], "hintFor": [serverId, ...]}.

    None if the item does not exist. `rev` backs the optimistic-concurrency conditional write.
    `hintFor` lists the down primaries this stand-in is holding writes for (empty for a normal replica).
    """
    item = DDB.get_item(TableName=DATA_TABLE, ConsistentRead=True,
                        Key={"serverId": {"S": server_id}, "itemKey": {"S": key}}).get("Item")
    if not item:
        return None
    hint_for = json.loads(item["hintFor"]["S"]) if "hintFor" in item else []
    return {"rev": int(item["rev"]["N"]), "versions": json.loads(item["versions"]["S"]), "hintFor": hint_for}


def _put_replica_item(server_id, key, versions, new_rev, prev_rev, hint_for):
    """Conditional PutItem: only if the stored rev still equals prev_rev (None = item absent).

    hint_for (list of intended down primaries) is written as the `hintFor` attribute when non-empty;
    an empty list omits the attribute, so the item reads back as a normal replica.
    """
    item = {"serverId": {"S": server_id}, "itemKey": {"S": key},
            "rev": {"N": str(new_rev)}, "versions": {"S": json.dumps(versions)}}
    if hint_for:
        item["hintFor"] = {"S": json.dumps(sorted(set(hint_for)))}
    kwargs = {"TableName": DATA_TABLE, "Item": item}
    if prev_rev is None:
        kwargs["ConditionExpression"] = "attribute_not_exists(itemKey)"
    else:
        kwargs["ConditionExpression"] = "rev = :r"
        kwargs["ExpressionAttributeValues"] = {":r": {"N": str(prev_rev)}}
    DDB.put_item(**kwargs)


def scan_hint_items(limit):
    """Scan the data table for stand-in items that still carry a `hintFor` attribute.

    Bounded: reads at most `limit` matching items. Returns (items, more) where each item is
    {"serverId", "itemKey", "rev", "versions", "hintFor"} and `more` is True if the scan was cut short.
    """
    items, more = [], False
    kwargs = {"TableName": DATA_TABLE, "FilterExpression": "attribute_exists(hintFor)"}
    while True:
        page = DDB.scan(**kwargs)
        for it in page.get("Items", []):
            items.append({"serverId": it["serverId"]["S"], "itemKey": it["itemKey"]["S"],
                          "rev": int(it["rev"]["N"]), "versions": json.loads(it["versions"]["S"]),
                          "hintFor": json.loads(it["hintFor"]["S"])})
            if len(items) >= limit:
                more = "LastEvaluatedKey" in page
                return items, more
        if "LastEvaluatedKey" not in page:
            return items, more
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


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


def sloppy_targets(key):
    """Sloppy placement for a key.

    Returns (pref, targets, empty). `pref` is the N-server preference list (the primaries). `targets`
    is a list of {"server", "primary", "standin"}: one per primary, in preference-list order. For an
    up primary, server == primary and standin is False. For a down primary, server is the next healthy
    distinct physical server clockwise not already a target (a stand-in), standin is True; if no healthy
    stand-in is available the entry's server is None. `empty` is True when the ring has no servers.
    """
    ring, status = load_cluster()
    if not ring:
        return [], [], True
    pref = preference_list(ring, position(key), N)
    up = lambda s: status.get(s, "up") == "up"
    chosen = set()
    targets = []
    for primary in pref:
        if up(primary):
            chosen.add(primary)
            targets.append({"server": primary, "primary": primary, "standin": False})
            continue
        # walk the ring clockwise for the next healthy distinct server not already a target
        stand = None
        start = next(i for i, v in enumerate(ring) if v[1] == primary)
        # start the walk just past the primary's first vnode; distinctness handles ordering
        for step in range(len(ring)):
            cand = ring[(start + step) % len(ring)][1]
            if cand in pref or cand in chosen or not up(cand):
                continue
            stand = cand
            break
        if stand is not None:
            chosen.add(stand)
        targets.append({"server": stand, "primary": primary, "standin": True})
    return pref, targets, False


def pick_coordinator(query, pref, targets):
    """Chosen coordinator serverId, or a (code, body) error tuple.

    Default is the first target that has a live server (the first up primary, or its stand-in).
    ?coordinator= overrides it but must be in the preference list (else 400) and resolvable to a live
    target server — i.e. up, or down with a healthy stand-in (else 503).
    """
    live = [t["server"] for t in targets if t["server"] is not None]
    requested = query.get("coordinator")
    if requested is None:
        return live[0] if live else (503, {"error": "no healthy server available to coordinate"})
    if requested not in pref:
        return 400, {"error": f"coordinator {requested} is not in the preference list {pref}"}
    t = next(t for t in targets if t["primary"] == requested)
    if t["server"] is None:
        return 503, {"error": f"coordinator {requested} is down and no stand-in is available"}
    return t["server"]


def write_replica(server_id, key, new_version, hint_primary):
    """Apply new_version to one replica with a bounded optimistic-concurrency retry.

    hint_primary is the intended down primary when this write goes to a stand-in, else None. On a
    stand-in write hint_primary is merged into the item's `hintFor` list even if the versions did not
    change, so the hint is recorded for replay. Returns "written" or "nochange"; raises on repeated
    conflicts.
    """
    for _ in range(REPLICA_WRITE_RETRIES):
        current = get_replica(server_id, key)
        stored = current["versions"] if current else []
        prev_rev = current["rev"] if current else None
        existing_hints = current["hintFor"] if current else []
        merged, changed = apply_write(stored, new_version)
        hint_for = list(existing_hints)
        hint_changed = False
        if hint_primary is not None and hint_primary not in hint_for:
            hint_for.append(hint_primary)
            hint_changed = True
        if not changed and not hint_changed:
            return "nochange"
        try:
            _put_replica_item(server_id, key, merged, (prev_rev or 0) + 1, prev_rev, hint_for)
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

    pref, targets, empty = sloppy_targets(key)
    if empty:
        return resp(409, {"error": "ring is empty; add a server first"})
    coord = pick_coordinator(query, pref, targets)
    if isinstance(coord, tuple):
        code, cbody = coord
        return resp(code, {"key": key, "preferenceList": pref, **cbody})

    live = [t for t in targets if t["server"] is not None]
    info = {"key": key, "position": to_hex(position(key)), "preferenceList": pref,
            "coordinator": coord, "n": len(pref), "nConfigured": N, "w": w}
    unavailable = [{"serverId": t["primary"], "reason": "down, no stand-in available"}
                   for t in targets if t["server"] is None]
    if len(live) < w:
        return resp(503, {**info, "error": f"only {len(live)} of {len(pref)} replica targets healthy, "
                                            f"need w={w}; write not attempted",
                          "acked": [], "failed": unavailable})

    # New clock = context clock with the coordinator's own entry incremented, then truncated.
    now = datetime.now(timezone.utc).isoformat()
    new_clock = vc.truncate(vc.increment(context_clock, coord, now), MAX_CLOCK_ENTRIES)
    new_version = {"value": value, "clock": new_clock, "timestamp": now, "coordinator": coord}

    by_server = {t["server"]: t for t in live}
    writes = fan_out(lambda s: write_replica(s, key, new_version,
                                             by_server[s]["primary"] if by_server[s]["standin"] else None),
                     list(by_server))
    acked, failed = [], list(unavailable)
    for t in live:
        s = t["server"]
        entry = {"serverId": s, "primary": t["primary"], "standIn": t["standin"]}
        if writes[s][1] is None:
            acked.append(entry)
        else:
            failed.append({**entry, "reason": writes[s][1]})
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

    pref, targets, empty = sloppy_targets(key)
    if empty:
        return resp(409, {"error": "ring is empty; add a server first"})
    info = {"key": key, "preferenceList": pref, "n": len(pref), "nConfigured": N, "r": r}
    live = [t for t in targets if t["server"] is not None]
    reads = fan_out(lambda s: get_replica(s, key), [t["server"] for t in live])

    # Count the first r successful responses in preference-list (target) order; report every target.
    replicas, counted, collected = [], 0, []
    for t in targets:
        role = "primary" if not t["standin"] else "standIn"
        base = {"serverId": t["server"], "role": role}
        if t["standin"]:
            base["standInFor"] = t["primary"]
        if t["server"] is None:
            replicas.append({**base, "status": "down", "counted": False})
            continue
        rec, err = reads[t["server"]]
        if err:
            replicas.append({**base, "status": "error", "error": err, "counted": False})
            continue
        use = counted < r
        counted += use
        versions = rec["versions"] if rec else []
        if use:
            collected.extend(versions)
        replicas.append({**base, "status": "ok", "found": rec is not None,
                         "versions": version_views(versions), "counted": bool(use)})
    if counted < r:
        return resp(503, {**info, "error": f"{counted} replica targets responded, need r={r}", "replicas": replicas})

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


# ---------- Hinted handoff replay ----------

def replay_one_item(item, up_servers):
    """Hand off a single stand-in item's hints to any now-up primaries it lists.

    For each intended primary in the item's `hintFor` that is up: write the stand-in's versions to
    that primary (a normal vector-clock merge write) and, on success, remove it from `hintFor`.
    Updates the stand-in item with the shrunk `hintFor` under a conditional write on `rev` so a
    concurrent write is not clobbered. Returns a list of {serverId (the primary), replayed bool}.
    """
    server_id, key = item["serverId"], item["itemKey"]
    hint_for = item["hintFor"]
    remaining = list(hint_for)
    handed = []
    for primary in list(hint_for):
        if primary not in up_servers:
            continue
        ok = True
        for v in item["versions"]:
            try:
                write_replica(primary, key, v, None)   # merge each sibling into the real primary
            except Exception:
                ok = False
                break
        handed.append({"serverId": primary, "replayed": ok})
        if ok:
            remaining.remove(primary)
    if remaining != hint_for:
        # rewrite the stand-in item with the shrunk hint list (idempotent: no-op if rev moved on)
        try:
            _put_replica_item(server_id, key, item["versions"], item["rev"] + 1, item["rev"], remaining)
        except DDB.exceptions.ConditionalCheckFailedException:
            pass  # a concurrent write changed the item; its own hints still get replayed next pass
    return handed


def replay_hints(target_server, up_servers):
    """Scan stand-in items and replay hints for now-up primaries.

    If target_server is given, only hints destined for it are replayed (and only if it is up).
    Otherwise every hint whose intended primary is up is replayed. Bounded by HINT_SCAN_LIMIT and
    HINT_REPLAY_LIMIT. Returns a summary dict.
    """
    items, more = scan_hint_items(HINT_SCAN_LIMIT)
    replayed_by_server, keys, replays = {}, 0, 0
    for item in items:
        if replays >= HINT_REPLAY_LIMIT:
            more = True
            break
        relevant = [p for p in item["hintFor"]
                    if (target_server is None or p == target_server) and p in up_servers]
        if not relevant:
            continue
        # narrow the item to just the relevant hints for this pass
        handed = replay_one_item({**item, "hintFor": relevant}, up_servers)
        done = False
        for h in handed:
            if h["replayed"]:
                replayed_by_server[h["serverId"]] = replayed_by_server.get(h["serverId"], 0) + 1
                replays += 1
                done = True
        if done:
            keys += 1
    return {"hintsReplayed": replays, "keysReplayed": keys,
            "byServer": replayed_by_server, "scanned": len(items), "more": more}


def server_up(server_id):
    """POST /servers/{serverId}/up: mark the server up, then replay hints destined for it."""
    if not SERVER_ID_RE.match(server_id or ""):
        return resp(400, {"error": "invalid serverId"})
    if not set_server_status(server_id, "up"):
        return resp(404, {"error": f"server {server_id} not found"})
    _, status = load_cluster()
    up_servers = {s for s, st in status.items() if st == "up"}
    summary = replay_hints(server_id, up_servers)
    return resp(200, {"serverId": server_id, "status": "up", "hintReplay": summary})


def hints_replay(event):
    """POST /hints/replay: manual/test replay. Optional body {"serverId": "..."} limits the target."""
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return resp(400, {"error": "body must be JSON"})
    if not isinstance(body, dict):
        return resp(400, {"error": "body must be a JSON object"})
    target = body.get("serverId")
    if target is not None and (not isinstance(target, str) or not SERVER_ID_RE.match(target)):
        return resp(400, {"error": "serverId must be 1-64 chars of A-Z a-z 0-9 _ . -"})
    _, status = load_cluster()
    up_servers = {s for s, st in status.items() if st == "up"}
    if target is not None and target not in up_servers:
        return resp(409, {"error": f"server {target} is not up; bring it up before replaying its hints"})
    summary = replay_hints(target, up_servers)
    return resp(200, {"target": target, "hintReplay": summary})


def handler(event, context):
    method = event.get("httpMethod")
    resource = event.get("resource")
    params = event.get("pathParameters") or {}
    query = event.get("queryStringParameters") or {}
    if resource == "/servers/{serverId}/up" and method == "POST":
        return server_up(params.get("serverId"))
    if resource == "/hints/replay" and method == "POST":
        return hints_replay(event)
    if resource == "/kv/{key}" and method in ("PUT", "GET"):
        key = params.get("key") or ""
        if not KEY_RE.match(key):
            return resp(400, {"error": "key must be 1-256 chars of A-Z a-z 0-9 _ . : -"})
        return put_key(key, event, query) if method == "PUT" else get_key(key, query)
    return resp(405, {"error": "method not allowed"})
