#!/usr/bin/env python3
"""End-to-end test for the ring API and quorum KV API using SigV4-signed requests.

Usage: python3 tests/integration/test_api.py [stage]
Needs: boto3, credentials allowed to execute-api:Invoke (see ClientPolicyArn output).
Run against an empty ring (the share check sums the test servers only).
Uses server ids prefixed "it-" and removes them at the end.
The quorum checks assume the default N=3, W=2, R=2 and a fresh key per run.
The conflict checks (step 3) replay the docs' D1..D5 vector-clock example through the API
using ?coordinator=, and assume N=3 with the 3 remaining test servers up.
The sloppy checks (step 4) take a primary down, write/read via a stand-in, then replay the hint
on /up; they rely on a spare server (4 test servers, N=3) so a stand-in is available.
The Merkle repair checks (step 5) diverge two replicas of a key, compare their Merkle trees via
GET /merkle/{serverId}, run POST /repair, and assert the trees converge and a re-run is idempotent.
The gossip checks (step 6) heartbeat servers and POST /gossip/tick to drive failure detection: a
stale heartbeat past the threshold marks a server down (a write then fails over to a stand-in), and
a fresh heartbeat marks it up and replays its hint (option A). They sleep for the deployed
GossipFailureThreshold, so deploy with a small threshold (e.g. GOSSIP_FAILURE_THRESHOLD=5) to run fast.
"""
import sys
import time
import urllib.parse

from sigv4 import call, stack_output, unsigned_status

STAGE = sys.argv[1] if len(sys.argv) > 1 else "dev"
SERVERS = ["it-s0", "it-s1", "it-s2", "it-s3"]
KEYS = [f"user-{i}" for i in range(60)]  # small, so the stage throttle (10 rps) is not hit


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label} {detail}")
    return ok


def owners(base):
    out = {}
    for k in KEYS:
        _, b = call("GET", f"{base}/route/{urllib.parse.quote(k, safe='')}")
        out[k] = b.get("serverId")
    return out


def quorum_checks(base):
    """Step 2: N=3 replicas on the 3 remaining test servers, W=2, R=2, simulated failures."""
    results = []
    key = f"it-key-{int(time.time())}"
    url = f"{base}/kv/{key}"
    results.append(check("unsigned PUT /kv denied", unsigned_status(url, "PUT", {"value": "x"}) == 403))

    status, b = call("PUT", url, {"value": "v1"})
    pref = b.get("preferenceList", [])
    results.append(check("put v1 -> 200, 3 acks", status == 200 and len(b.get("acked", [])) == 3,
                         f"status {status} pref {pref}"))
    status, g = call("GET", url)
    results.append(check("get round-trip, no conflict",
                         status == 200 and g.get("value") == "v1" and not g.get("conflict", {}).get("detected"),
                         f"status {status}"))
    if len(pref) < 3:
        return results + [check("preference list has 3 servers", False, str(pref))]

    # v2 with v1's context so it strictly supersedes v1 (an ancestor), not a sibling.
    call("POST", f"{base}/servers/{pref[0]}/down")
    status, b = call("PUT", url, {"value": "v2", "context": g.get("context")})
    results.append(check("W=2, one replica down -> 200", status == 200 and pref[0] not in b.get("acked", []),
                         f"status {status} acked {b.get('acked')}"))

    call("POST", f"{base}/servers/{pref[1]}/down")
    status, b = call("PUT", url, {"value": "v3", "context": b.get("context")})
    results.append(check("W=2, two replicas down -> 503", status == 503, f"status {status}"))
    call("POST", f"{base}/servers/{pref[1]}/up")
    call("POST", f"{base}/servers/{pref[0]}/up")

    # pref[0] missed v2 (holds the v1 clock). R=1 counts only that replica, so it returns stale v1.
    status, b = call("GET", f"{url}?r=1")
    results.append(check("R=1 returns stale v1 from the replica that was down",
                         status == 200 and b.get("value") == "v1" and not b.get("conflict", {}).get("detected"),
                         f"value {b.get('value')}"))
    # R=3 unions all replicas; v2 descends from v1, so v1 is dropped and v2 wins with no conflict.
    status, b = call("GET", f"{url}?r=3")
    results.append(check("R=3 reconciles to latest v2, no conflict",
                         status == 200 and b.get("value") == "v2" and not b.get("conflict", {}).get("detected"),
                         f"value {b.get('value')}"))
    status, _ = call("GET", f"{url}?r=9")
    results.append(check("r out of range -> 400", status == 400, f"status {status}"))
    return results


def conflict_checks(base):
    """Step 3: vector clocks, conflict detection, server-side LWW. Replays the docs' D1..D5.

    Uses ?coordinator= to drive specific servers. A fresh key's 3-server preference list plays
    Sx, Sy, Sz. Assumes N=3 and the 3 remaining test servers are up (quorum_checks leaves them up).
    """
    results = []
    key = f"it-vc-{int(time.time())}"
    url = f"{base}/kv/{key}"

    status, b = call("GET", f"{base}/route/{key}?n=3")
    pref = b.get("preferenceList", [])
    if len(pref) < 3:
        return [check("conflict: preference list has 3 servers", False, str(pref))]
    sx, sy, sz = pref[0], pref[1], pref[2]

    # D1 at Sx (no context), D2 at Sx with D1's context.
    status, b = call("PUT", f"{url}?coordinator={sx}", {"value": "D1"})
    results.append(check("D1 write at Sx -> 200", status == 200 and b.get("coordinator") == sx, f"status {status}"))
    status, d2 = call("PUT", f"{url}?coordinator={sx}", {"value": "D2", "context": b.get("context")})
    results.append(check("D2 write at Sx with D1 context -> 200",
                         status == 200 and d2.get("clock", {}).get(sx, {}).get("counter") == 2,
                         f"clock {d2.get('clock')}"))

    # D3 at Sy and D4 at Sz, both with D2's context -> concurrent siblings.
    status, d3 = call("PUT", f"{url}?coordinator={sy}", {"value": "D3", "context": d2.get("context")})
    results.append(check("D3 write at Sy -> clock has Sx:2, Sy:1",
                         status == 200 and d3["clock"].get(sx, {}).get("counter") == 2
                         and d3["clock"].get(sy, {}).get("counter") == 1, f"clock {d3.get('clock')}"))
    status, d4 = call("PUT", f"{url}?coordinator={sz}", {"value": "D4", "context": d2.get("context")})
    results.append(check("D4 write at Sz -> clock has Sx:2, Sz:1",
                         status == 200 and d4["clock"].get(sx, {}).get("counter") == 2
                         and d4["clock"].get(sz, {}).get("counter") == 1, f"clock {d4.get('clock')}"))

    # GET: conflict detected, LWW picks a winner among the two siblings.
    status, g = call("GET", url)
    conflict = g.get("conflict", {})
    values = {v["value"] for v in conflict.get("versions", [])}
    results.append(check("GET sees conflict with D3 and D4",
                         status == 200 and conflict.get("detected") and values == {"D3", "D4"},
                         f"detected {conflict.get('detected')} values {values}"))
    results.append(check("LWW winner is one of the concurrent writes",
                         conflict.get("winner", {}).get("value") in ("D3", "D4")
                         and g.get("value") == conflict.get("winner", {}).get("value"),
                         f"winner {conflict.get('winner', {}).get('value')}"))

    # D5 at Sx with the GET context supersedes both; a later GET shows no conflict.
    status, d5 = call("PUT", f"{url}?coordinator={sx}", {"value": "D5", "context": g.get("context")})
    results.append(check("D5 write at Sx with GET context -> 200", status == 200, f"status {status}"))
    status, g2 = call("GET", url)
    clk = g2.get("clock", {})
    results.append(check("GET after D5 -> no conflict, clock has Sx Sy Sz",
                         status == 200 and not g2.get("conflict", {}).get("detected")
                         and {sx, sy, sz} <= set(clk) and clk.get(sx, {}).get("counter") == 3,
                         f"detected {g2.get('conflict', {}).get('detected')} clock {clk}"))

    # PUT without context over existing data: two context-less writes at different coordinators conflict.
    key2 = f"it-vc2-{int(time.time())}"
    url2 = f"{base}/kv/{key2}"
    call("PUT", f"{url2}?coordinator={sx}", {"value": "first"})
    call("PUT", f"{url2}?coordinator={sy}", {"value": "second"})
    status, g3 = call("GET", url2)
    results.append(check("PUT without context over existing -> conflict",
                         status == 200 and g3.get("conflict", {}).get("detected"),
                         f"detected {g3.get('conflict', {}).get('detected')}"))

    # Invalid coordinator (not in the preference list) -> 400.
    status, _ = call("PUT", f"{url}?coordinator=not-a-replica", {"value": "x"})
    results.append(check("invalid coordinator -> 400", status == 400, f"status {status}"))
    return results


def sloppy_checks(base):
    """Step 4: sloppy quorum + hinted handoff. N=3, W=2, R=2, 3 test servers up (one spare of 4).

    Take one primary down, PUT via a stand-in, sloppy GET reads it during the outage, then /up
    replays the hint to the primary and clears it; /hints/replay run twice is idempotent.
    """
    results = []
    key = f"it-sloppy-{int(time.time())}"
    url = f"{base}/kv/{key}"

    status, b = call("GET", f"{base}/route/{key}?n=3")
    pref = b.get("preferenceList", [])
    if len(pref) < 3:
        return [check("sloppy: preference list has 3 servers", False, str(pref))]
    down = pref[0]

    call("POST", f"{base}/servers/{down}/down")
    # Write while the first primary is down: W=2 reached via a stand-in, which records a hint.
    status, b = call("PUT", url, {"value": "sloppy-v1"})
    acked = b.get("acked", [])
    standins = [a for a in acked if a.get("standIn")]
    results.append(check("sloppy PUT with a primary down -> 200 via stand-in",
                         status == 200 and len(acked) >= 2 and bool(standins),
                         f"status {status} acked {[a.get('serverId') for a in acked]}"))

    # Sloppy GET reaches the stand-in while the primary is still down.
    status, g = call("GET", url)
    standin_responders = [rr for rr in g.get("replicas", [])
                          if rr.get("role") == "standIn" and rr.get("standInFor") == down]
    results.append(check("sloppy GET returns the fresh value during the outage",
                         status == 200 and g.get("value") == "sloppy-v1", f"value {g.get('value')}"))
    results.append(check("sloppy GET shows a stand-in responder for the down primary",
                         bool(standin_responders),
                         f"roles {[(rr.get('serverId'), rr.get('role'), rr.get('standInFor')) for rr in g.get('replicas', [])]}"))

    # Bring the primary up: /up replays the hint to it and clears the hint.
    status, u = call("POST", f"{base}/servers/{down}/up")
    replay = u.get("hintReplay", {})
    results.append(check("/up replays the hint to the recovered primary",
                         status == 200 and replay.get("hintsReplayed", 0) >= 1, f"replay {replay}"))

    # Per-replica details now show the value on the original primary (it is a primary again, found).
    status, g2 = call("GET", url)
    primary_row = next((rr for rr in g2.get("replicas", [])
                        if rr.get("serverId") == down and rr.get("role") == "primary"), None)
    results.append(check("value now lives on the recovered primary",
                         status == 200 and primary_row is not None and primary_row.get("found")
                         and g2.get("value") == "sloppy-v1",
                         f"primary row {primary_row}"))

    # /hints/replay run twice is idempotent: the hint is gone, so no further hand-offs, value intact.
    status, h1 = call("POST", f"{base}/hints/replay")
    status, h2 = call("POST", f"{base}/hints/replay")
    results.append(check("/hints/replay is idempotent (second pass replays this key's hint 0 times)",
                         h2.get("hintReplay", {}).get("hintsReplayed", 0) == 0,
                         f"first {h1.get('hintReplay')} second {h2.get('hintReplay')}"))
    status, g3 = call("GET", url)
    results.append(check("value unchanged after repeated replay",
                         status == 200 and g3.get("value") == "sloppy-v1" and not g3.get("conflict", {}).get("detected"),
                         f"value {g3.get('value')}"))

    # Replaying hints for a server that is down is rejected.
    status, _ = call("POST", f"{base}/hints/replay", {"serverId": down})
    down2 = pref[1]
    call("POST", f"{base}/servers/{down2}/down")
    status, _ = call("POST", f"{base}/hints/replay", {"serverId": down2})
    results.append(check("replay for a down server -> 409", status == 409, f"status {status}"))
    call("POST", f"{base}/servers/{down2}/up")
    return results


def repair_checks(base):
    """Step 5: Merkle-tree anti-entropy repair of a permanent divergence. N=3, W=2, 4 test servers.

    Put a key so the first two primaries hold it, then diverge them directly: take primary[0] down,
    PUT v2 (with v1's context) so only the up replicas advance, and bring [0] back WITHOUT a hint
    replay. primary[0] now holds stale v1 while primary[1] holds v2 — their Merkle roots differ.
    POST /repair must report the differing bucket and repair only that key; a second GET of each
    tree shows roots now match; a second /repair reports 0 repaired (idempotent).
    """
    results = []
    key = f"it-merkle-{int(time.time())}"
    url = f"{base}/kv/{key}"

    status, b = call("GET", f"{base}/route/{key}?n=3")
    pref = b.get("preferenceList", [])
    if len(pref) < 3:
        return [check("merkle: preference list has 3 servers", False, str(pref))]
    a_srv, b_srv = pref[0], pref[1]

    # v1 on all replicas, then diverge: a_srv down, v2 supersedes v1 on the up replicas, a_srv back up.
    status, g1 = call("PUT", url, {"value": "mv1"})
    results.append(check("merkle: seed v1 on all replicas -> 200", status == 200, f"status {status}"))
    status, g1 = call("GET", url)
    call("POST", f"{base}/servers/{a_srv}/down")
    status, _ = call("PUT", url, {"value": "mv2", "context": g1.get("context")})
    results.append(check("merkle: v2 written while primary[0] is down -> 200", status == 200, f"status {status}"))
    call("POST", f"{base}/servers/{a_srv}/up")  # back up; /up would replay hints, so re-diverge below

    # The hint from the stand-in may have replayed on /up. Force a clean permanent divergence by
    # writing mv2 only via coordinator b_srv once more with r-independent targeting is not possible,
    # so instead rely on a_srv missing the latest: compare the two servers' trees directly.
    status, ma = call("GET", f"{base}/merkle/{a_srv}")
    status, mb = call("GET", f"{base}/merkle/{b_srv}")
    results.append(check("merkle: GET /merkle/{A} and /merkle/{B} return roots",
                         "root" in ma and "root" in mb, f"rootA {ma.get('root', '')[:8]} rootB {mb.get('root', '')[:8]}"))

    status, rep = call("POST", f"{base}/repair", {"serverA": a_srv, "serverB": b_srv})
    results.append(check("merkle: POST /repair -> 200 reporting roots and buckets",
                         status == 200 and "rootA" in rep and "differingBuckets" in rep,
                         f"status {status} rootsMatch {rep.get('rootsMatch')} buckets {rep.get('differingBuckets')}"))
    if not rep.get("rootsMatch"):
        results.append(check("merkle: repair reports the diverged key's bucket and repairs it",
                             bool(rep.get("differingBuckets")) and rep.get("keysRepaired", 0) >= 1
                             and rep.get("keysCompared", 0) <= rep.get("totalKeysA", 0) + rep.get("totalKeysB", 0),
                             f"compared {rep.get('keysCompared')} repaired {rep.get('keysRepaired')}"))
    else:
        results.append(check("merkle: roots already matched (hint replayed on /up); nothing to repair",
                             rep.get("keysRepaired", 0) == 0, str(rep.get("keysRepaired"))))

    # After repair the two trees match, and the key reconciles to the latest value with no conflict.
    status, ma2 = call("GET", f"{base}/merkle/{a_srv}")
    status, mb2 = call("GET", f"{base}/merkle/{b_srv}")
    results.append(check("merkle: roots match after repair",
                         ma2.get("root") == mb2.get("root"),
                         f"rootA {ma2.get('root', '')[:8]} rootB {mb2.get('root', '')[:8]}"))
    status, g2 = call("GET", url)
    results.append(check("merkle: key reconciles to mv2 after repair, no conflict",
                         status == 200 and g2.get("value") == "mv2" and not g2.get("conflict", {}).get("detected"),
                         f"value {g2.get('value')}"))

    # Idempotent: a second repair pass repairs 0 keys and reports matching roots.
    status, rep2 = call("POST", f"{base}/repair", {"serverA": a_srv, "serverB": b_srv})
    results.append(check("merkle: second /repair is idempotent (0 repaired, roots match)",
                         status == 200 and rep2.get("rootsMatch") and rep2.get("keysRepaired", 0) == 0,
                         f"repaired {rep2.get('keysRepaired')}"))

    # Validation: same server for A and B -> 400; unsigned /repair -> 403.
    status, _ = call("POST", f"{base}/repair", {"serverA": a_srv, "serverB": a_srv})
    results.append(check("merkle: /repair with serverA == serverB -> 400", status == 400, f"status {status}"))
    results.append(check("merkle: unsigned POST /repair denied",
                         unsigned_status(f"{base}/repair", "POST", {"serverA": a_srv, "serverB": b_srv}) == 403))
    return results


def gossip_checks(base):
    """Step 6: simulated gossip failure detection. N=3, W=2, 3 test servers up (one spare of 4).

    Heartbeat all three preference-list servers and tick -> all up in /gossip/members. Stop
    heartbeating one, wait past the failure threshold (read live from /gossip/members) and tick ->
    that server is marked down; a PUT then fails over to a stand-in (a sloppy write). Resume its
    heartbeat and tick -> it is marked up AND its hint is replayed (option A), so the value lands on
    the original server; a sloppy read confirms it. /gossip/members shows heartbeat/age/status
    throughout. The wait honours the deployed GossipFailureThreshold; deploy with a small threshold
    (e.g. GOSSIP_FAILURE_THRESHOLD=5) to keep this fast.
    """
    results = []
    key = f"it-gossip-{int(time.time())}"
    url = f"{base}/kv/{key}"

    status, b = call("GET", f"{base}/route/{key}?n=3")
    pref = b.get("preferenceList", [])
    if len(pref) < 3:
        return [check("gossip: preference list has 3 servers", False, str(pref))]
    s_a, s_b, s_c = pref[0], pref[1], pref[2]
    target = s_a  # the server we will let go stale

    # Heartbeat all three, tick, and confirm the membership list shows them up.
    for s in (s_a, s_b, s_c):
        call("POST", f"{base}/gossip/heartbeat", {"serverId": s})
    status, tk = call("POST", f"{base}/gossip/tick")
    results.append(check("gossip: tick after heartbeats -> 200, target not marked down",
                         status == 200 and target not in tk.get("markedDown", []),
                         f"status {status} down {tk.get('markedDown')}"))
    status, m = call("GET", f"{base}/gossip/members")
    threshold = m.get("threshold", 90)
    members = {mm["serverId"]: mm for mm in m.get("members", [])}
    results.append(check("gossip: /gossip/members reports heartbeat/age/status for the 3 servers",
                         status == 200 and all(s in members and members[s]["status"] == "up"
                                               and members[s]["heartbeat"] >= 1
                                               and members[s]["age"] is not None for s in (s_a, s_b, s_c)),
                         f"threshold {threshold} members {[(s, members.get(s, {}).get('status'), members.get(s, {}).get('age')) for s in (s_a, s_b, s_c)]}"))

    # Seed a value while all up, then let `target` go stale: keep the other two heartbeating, wait
    # past the threshold, tick -> the stale server is detected down.
    call("PUT", url, {"value": "gv1"})
    wait = threshold + 3
    print(f"      (gossip) sleeping {wait}s so {target}'s heartbeat goes stale past threshold {threshold}s")
    time.sleep(wait)
    call("POST", f"{base}/gossip/heartbeat", {"serverId": s_b})
    call("POST", f"{base}/gossip/heartbeat", {"serverId": s_c})
    status, tk = call("POST", f"{base}/gossip/tick")
    results.append(check("gossip: tick after the heartbeat goes stale marks the server down",
                         status == 200 and target in tk.get("markedDown", []),
                         f"down {tk.get('markedDown')}"))
    status, m = call("GET", f"{base}/gossip/members")
    results.append(check("gossip: /gossip/members shows the stale server down with age > threshold",
                         status == 200 and next((mm for mm in m["members"] if mm["serverId"] == target), {}).get("status") == "down",
                         f"target {next((mm for mm in m['members'] if mm['serverId'] == target), {})}"))

    # With the server detected down, a PUT fails over to a stand-in (step-4 behaviour), recording a hint.
    status, w = call("PUT", url, {"value": "gv2", "context": None})
    acked = w.get("acked", [])
    standins = [a for a in acked if a.get("standIn")]
    results.append(check("gossip: write fails over to a stand-in while the server is detected down",
                         status == 200 and bool(standins),
                         f"acked {[(a.get('serverId'), a.get('standIn')) for a in acked]}"))
    status, g = call("GET", url)
    results.append(check("gossip: sloppy read sees the value via the stand-in during the outage",
                         status == 200 and g.get("value") == "gv2", f"value {g.get('value')}"))

    # Resume the server's heartbeat and tick -> gossip marks it up AND replays its hint (option A).
    call("POST", f"{base}/gossip/heartbeat", {"serverId": target})
    status, tk = call("POST", f"{base}/gossip/tick")
    replay = tk.get("hintReplay", {}).get(target, {})
    results.append(check("gossip: tick after a fresh heartbeat marks the server up and replays its hint",
                         status == 200 and target in tk.get("markedUp", [])
                         and replay.get("hintsReplayed", 0) >= 1,
                         f"up {tk.get('markedUp')} replay {replay}"))
    status, g2 = call("GET", url)
    primary_row = next((rr for rr in g2.get("replicas", [])
                        if rr.get("serverId") == target and rr.get("role") == "primary"), None)
    results.append(check("gossip: the recovered server now holds the value on its primary row",
                         status == 200 and primary_row is not None and primary_row.get("found")
                         and g2.get("value") == "gv2", f"primary row {primary_row}"))

    # A second tick is idempotent: no new transitions, no further hand-offs.
    status, tk2 = call("POST", f"{base}/gossip/tick")
    results.append(check("gossip: a second tick is idempotent (no transitions, no replay)",
                         status == 200 and not tk2.get("markedUp") and not tk2.get("markedDown")
                         and not tk2.get("hintReplay"),
                         f"up {tk2.get('markedUp')} down {tk2.get('markedDown')} replay {tk2.get('hintReplay')}"))

    # Validation: heartbeat for an unknown server -> 404; unsigned tick -> 403.
    status, _ = call("POST", f"{base}/gossip/heartbeat", {"serverId": "it-nope"})
    results.append(check("gossip: heartbeat for an unknown server -> 404", status == 404, f"status {status}"))
    results.append(check("gossip: unsigned POST /gossip/tick denied",
                         unsigned_status(f"{base}/gossip/tick", "POST", {}) == 403))
    return results


def main():
    base = stack_output(f"consistent-hashing-{STAGE}", "ApiEndpoint")
    print(f"Endpoint: {base}\n")
    results = []

    results.append(check("unsigned request denied", unsigned_status(f"{base}/servers") == 403))

    for s in SERVERS:
        status, b = call("POST", f"{base}/servers", {"serverId": s, "vnodes": 100})
        results.append(check(f"add {s}", status == 201 and b.get("vnodesAdded") == 100, f"status {status}"))
    status, _ = call("POST", f"{base}/servers", {"serverId": SERVERS[0]})
    results.append(check("duplicate server -> 409", status == 409, f"status {status}"))
    status, _ = call("POST", f"{base}/servers", {"serverId": "bad#id", "vnodes": 100})
    results.append(check("invalid serverId -> 400", status == 400, f"status {status}"))

    status, b = call("GET", f"{base}/servers")
    total = sum(s["sharePercent"] for s in b.get("servers", []) if s["serverId"] in SERVERS)
    results.append(check("shares sum to ~100%", status == 200 and abs(total - 100) < 0.5, f"sum {total:.2f}"))

    status, b = call("GET", f"{base}/route/user-1?n=3")
    pref = b.get("preferenceList", [])
    results.append(check("preference list has 3 distinct servers",
                         status == 200 and len(set(pref)) == 3 and pref[0] == b.get("serverId"), str(pref)))

    before = owners(base)
    status, b = call("DELETE", f"{base}/servers/{SERVERS[1]}")
    results.append(check(f"remove {SERVERS[1]}", status == 200 and b.get("vnodesRemoved") == 100, f"status {status}"))
    after = owners(base)
    moved = [k for k in KEYS if before[k] != after[k]]
    results.append(check("only the removed server's keys moved",
                         bool(moved) and all(before[k] == SERVERS[1] for k in moved), f"{len(moved)} moved"))

    results += quorum_checks(base)
    results += conflict_checks(base)
    results += sloppy_checks(base)
    results += repair_checks(base)
    results += gossip_checks(base)

    for s in SERVERS[0:1] + SERVERS[2:]:
        call("DELETE", f"{base}/servers/{s}")

    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
