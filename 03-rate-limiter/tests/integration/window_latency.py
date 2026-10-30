"""Live test for the two window algorithms: correctness + boundary + latency per language.

Usage:
    python3 tests/integration/window_latency.py http://<ec2-ip>:8080 <algorithm> [requests_per_language]
    algorithm: fixed_window_counter or sliding_window_log

No installs needed (standard library only). Run it from the laptop whose IP
you allowed in the security group.

What it does, for each language (python, go, node), one at a time:
  1. Limit check: 12 parallel requests as one fresh user, just after a window
     starts -> expect exactly 10 allowed and 2 x 429 by the "user" rule.
  2. Boundary test: 10 parallel requests ~100 ms before a window boundary,
     then 10 right after it. Fixed window allows ~20 (two windows), sliding
     log allows only 10 (it looks at the last 1 s, wherever it starts).
  3. Latency run: N requests paced under every limit (fresh user each time),
     printing p50/p90/p99 of client, total, redis and backend.
Exits non-zero on any unexpected result.
"""

import http.client  # keep-alive HTTP connection (no new TCP handshake per request)
import json  # parse response bodies
import statistics  # quantiles for p50/p90/p99
import sys  # command-line arguments
import threading  # parallel requests fired at an exact moment
import time  # timing and pacing
import urllib.parse  # split the endpoint URL
import uuid  # unique user ids per run

# Languages behind the router, tested one at a time.
LANGS = ["python", "go", "node"]
# The two algorithms this script knows.
ALGOS = ["fixed_window_counter", "sliding_window_log"]


def parse_timing(header):
    """'rules;dur=0.02, redis;dur=0.4' -> {'rules': 0.02, 'redis': 0.4} (ms)."""
    # Collected durations.
    out = {}
    # One "name;dur=x" part at a time.
    for part in (header or "").split(","):
        name, _, dur = part.strip().partition(";dur=")
        if name and dur:
            out[name] = float(dur)
    return out


class Client:
    """One persistent HTTP connection to the router."""

    def __init__(self, endpoint):
        # Host and port from the endpoint URL.
        u = urllib.parse.urlparse(endpoint)
        self.host, self.port = u.hostname, u.port or 80
        # Not connected yet; connect() or the first request opens it.
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=10)

    def get(self, path, headers):
        """Send one GET; return (status, client_ms, server_timing, body)."""
        # Start the clock.
        t0 = time.perf_counter()
        # Send and read the whole answer.
        self.conn.request("GET", path, headers=headers)
        resp = self.conn.getresponse()
        body = resp.read()
        # Round trip in ms.
        ms = (time.perf_counter() - t0) * 1000
        return resp.status, ms, parse_timing(resp.getheader("Server-Timing")), body


def fire(endpoint, path, user, count, at):
    """Send `count` parallel requests as `user` so they reach the server at wall time `at`.

    Each request gets its own connection, opened BEFORE the moment, so the TCP
    handshake doesn't delay it. Returns a list of (status, body).
    """
    # One open connection per request.
    clients = [Client(endpoint) for _ in range(count)]
    for c in clients:
        c.conn.connect()
    # Results, filled in by the threads.
    results = [None] * count

    def one(i):
        # Wait for the moment, then send.
        time.sleep(max(0, at - time.time()))
        status, _, _, body = clients[i].get(path, {"X-User-Id": user})
        results[i] = (status, body)

    # Start all threads, wait for all.
    threads = [threading.Thread(target=one, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def summary(results):
    """(allowed count, 429 count, set of denying rules)."""
    statuses = [s for s, _ in results]
    rules = {json.loads(b or b"{}").get("rule", "") for s, b in results if s == 429}
    return statuses.count(200), statuses.count(429), rules


def limit_check(endpoint, lang, run_id, one_way_s):
    """12 parallel requests as one fresh user, arriving ~50 ms after a window starts."""
    # Next whole second + 50 ms, minus the trip to the server.
    at = int(time.time()) + 1 + 0.05 - one_way_s
    allowed, denied, rules = summary(fire(endpoint, f"/{lang}/orders", f"lim{run_id}{lang}", 12, at))
    ok = allowed == 10 and denied == 2 and rules == {"user"}
    print(f"  limit check: {allowed}/12 allowed, {denied} x 429 by {sorted(rules)} -> {'OK' if ok else 'UNEXPECTED'}")
    return ok


def boundary_test(endpoint, lang, run_id, algo, one_way_s):
    """10 parallel just before a window boundary, 10 just after; count what got through."""
    # The next whole second is the boundary (all 1 s windows start on whole seconds).
    boundary = int(time.time()) + 2
    path, user = f"/{lang}/orders", f"bnd{run_id}{lang}"
    # Results of the two batches.
    res = {}

    def batch(name, at):
        # One batch of 10 parallel requests arriving at `at`.
        res[name] = fire(endpoint, path, user, 10, at)

    # Batch A arrives ~100 ms before the boundary, batch B ~50 ms after it.
    ta = threading.Thread(target=batch, args=("a", boundary - 0.1 - one_way_s))
    tb = threading.Thread(target=batch, args=("b", boundary + 0.05 - one_way_s))
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    # Allowed per batch.
    a, b = summary(res["a"])[0], summary(res["b"])[0]
    # Fixed window: two windows -> up to 20. Sliding log: only 10 in any 1 s.
    want = 20 if algo == "fixed_window_counter" else 10
    ok = a + b == want
    print(f"  boundary test: {a} + {b} = {a + b}/20 allowed in ~150 ms (expect {want}) -> {'OK' if ok else 'UNEXPECTED'}")
    return ok, a + b


def pct(values, p):
    """p-th percentile (p in 1..99) of a list, or None if empty."""
    if len(values) < 2:
        return values[0] if values else None
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]


def fmt(v):
    """A number as 7 wide with 2 decimals, or a dash."""
    return "   -   " if v is None else f"{v:7.2f}"


def latency_run(c, lang, n, run_id):
    """N requests at ~6/s (under the shared ip limit of 20/s), fresh user each time."""
    client, total, redis, backend, statuses = [], [], [], [], []
    # Seconds between requests.
    gap = 1 / 6
    for i in range(n):
        start = time.perf_counter()
        status, ms, st, _ = c.get(f"/{lang}/orders", {"X-User-Id": f"lat{run_id}{lang}{i}"})
        statuses.append(status)
        # Only allowed requests went through every step.
        if status == 200:
            client.append(ms)
            total.append(st.get("total", 0))
            redis.append(st.get("redis", 0))
            backend.append(st.get("backend", 0))
        # Pace the next request.
        time.sleep(max(0, gap - (time.perf_counter() - start)))
    print(f"  latency run: {statuses.count(200)}/{n} allowed (others: {sorted(set(statuses) - {200}) or 'none'})")
    print("               p50      p90      p99   (ms)")
    for name, vals in [("client", client), ("total", total), ("redis", redis), ("backend", backend)]:
        print(f"    {name:8} {fmt(pct(vals, 50))}  {fmt(pct(vals, 90))}  {fmt(pct(vals, 99))}")
    ok = statuses.count(200) == n
    return ok, {"lang": lang, "client_p50": pct(client, 50), "client_p99": pct(client, 99),
                "total_p50": pct(total, 50), "redis_p50": pct(redis, 50), "backend_p50": pct(backend, 50)}


def main():
    # Endpoint and algorithm are required.
    if len(sys.argv) < 3 or sys.argv[2] not in ALGOS:
        print(__doc__)
        sys.exit(2)
    endpoint, algo = sys.argv[1].rstrip("/"), sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    run_id = uuid.uuid4().hex[:6]
    c = Client(endpoint)

    # Router reachable? Warm up, then take half the best round trip as the one-way time.
    c.get("/healthz", {})
    rtt = min(c.get("/healthz", {})[1] for _ in range(5))
    one_way_s = rtt / 2000
    print(f"router /healthz: best round trip {rtt:.1f} ms; algorithm {algo}")

    results, all_ok, boundary = [], True, {}
    for lang in LANGS:
        print(f"\n== {lang}")
        all_ok &= limit_check(endpoint, lang, run_id, one_way_s)
        # Let the shared ip window empty (1 s) before the next step.
        time.sleep(1.5)
        ok, boundary[lang] = boundary_test(endpoint, lang, run_id, algo, one_way_s)
        all_ok &= ok
        time.sleep(1.5)
        ok, row = latency_run(c, lang, n, run_id)
        all_ok &= ok
        results.append(row)
        time.sleep(1.5)

    # The key teaching result.
    print(f"\nBoundary test ({algo}): allowed per language {boundary} out of 20 sent around one boundary")
    # Markdown rows for the README latency table.
    print("\n| Language | client p50 | client p99 | server total p50 | redis p50 | backend p50 |")
    print("|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['lang']} | {fmt(r['client_p50'])} | {fmt(r['client_p99'])} | "
              f"{fmt(r['total_p50'])} | {fmt(r['redis_p50'])} | {fmt(r['backend_p50'])} |")
    print("\nAll checks OK" if all_ok else "\nSome checks were UNEXPECTED")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
