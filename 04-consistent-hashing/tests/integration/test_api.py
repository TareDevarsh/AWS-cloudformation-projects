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

    for s in SERVERS[0:1] + SERVERS[2:]:
        call("DELETE", f"{base}/servers/{s}")

    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
