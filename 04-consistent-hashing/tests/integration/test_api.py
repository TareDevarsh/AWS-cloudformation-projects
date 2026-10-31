#!/usr/bin/env python3
"""End-to-end test for the ring API using SigV4-signed requests.

Usage: python3 tests/integration/test_api.py [stage]
Needs: boto3, credentials allowed to execute-api:Invoke (see ClientPolicyArn output).
Run against an empty ring (the share check sums the test servers only).
Uses server ids prefixed "it-" and removes them at the end.
"""
import sys
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

    for s in SERVERS[0:1] + SERVERS[2:]:
        call("DELETE", f"{base}/servers/{s}")

    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
