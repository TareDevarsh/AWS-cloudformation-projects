"""Live test for the deployed rate limiter: correctness + latency per language.

Usage:
    python3 tests/integration/latency.py http://<ec2-ip>:8080 [requests_per_language]

No installs needed (standard library only). Run it from the laptop whose IP
you allowed in the security group.

What it does, for each language (python, go, node), one at a time:
  1. Limit check: 12 quick requests as one user -> expect 10 allowed, then 429
     (the "user" bucket holds 10).
  2. Latency run: N requests paced under every limit (a fresh user id each
     time, slower than the shared "ip" bucket refills), so every request is
     allowed and reaches the backend.
  3. Prints p50/p90/p99 of:
       client  round trip measured here (includes your internet link)
       total   time inside the limiter (from Server-Timing)
       redis   the Redis call (token_bucket.lua)
       backend the Lambda invoke
"""

import http.client  # keep-alive HTTP connection (no new TCP handshake per request)
import json  # parse response bodies
import statistics  # quantiles for p50/p90/p99
import sys  # command-line arguments
import time  # timing and pacing
import urllib.parse  # split the endpoint URL
import uuid  # unique user ids per run

LANGS = ["python", "go", "node"]


def parse_timing(header):
    """'rules;dur=0.02, redis;dur=0.4' -> {'rules': 0.02, 'redis': 0.4} (ms)."""
    out = {}
    for part in (header or "").split(","):
        name, _, dur = part.strip().partition(";dur=")
        if name and dur:
            out[name] = float(dur)
    return out


class Client:
    """One persistent HTTP connection to the router."""

    def __init__(self, endpoint):
        u = urllib.parse.urlparse(endpoint)
        self.host, self.port = u.hostname, u.port or 80
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=10)

    def get(self, path, headers):
        """Send one GET; return (status, client_ms, server_timing, body)."""
        t0 = time.perf_counter()
        try:
            self.conn.request("GET", path, headers=headers)
            resp = self.conn.getresponse()
            body = resp.read()
        except (http.client.HTTPException, OSError):
            # Connection dropped: reconnect once and retry.
            self.conn.close()
            self.conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
            self.conn.request("GET", path, headers=headers)
            resp = self.conn.getresponse()
            body = resp.read()
        ms = (time.perf_counter() - t0) * 1000
        return resp.status, ms, parse_timing(resp.getheader("Server-Timing")), body


def pct(values, p):
    """p-th percentile (p in 1..99) of a list, or None if empty."""
    if len(values) < 2:
        return values[0] if values else None
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]


def fmt(v):
    return "   -   " if v is None else f"{v:7.2f}"


def limit_check(c, lang, run_id):
    """12 fast requests as one user: the user bucket (size 10) should allow 10."""
    statuses = []
    for _ in range(12):
        status, _, _, body = c.get(f"/{lang}/orders", {"X-User-Id": f"burst{run_id}{lang}"})
        statuses.append(status)
    allowed = statuses.count(200)
    rule = ""
    if 429 in statuses:
        rule = json.loads(body or b"{}").get("rule", "")
    ok = allowed == 10 and statuses[-1] == 429 and rule == "user"
    print(f"  limit check: {allowed}/12 allowed, last={statuses[-1]} rule={rule!r} -> {'OK' if ok else 'UNEXPECTED'}")
    return ok


def latency_run(c, lang, n, run_id):
    """N requests at ~6/s (under the shared ip bucket's 10/s), fresh user each time."""
    client, total, redis, backend, statuses = [], [], [], [], []
    gap = 1 / 6
    for i in range(n):
        start = time.perf_counter()
        status, ms, st, _ = c.get(f"/{lang}/orders", {"X-User-Id": f"lat{run_id}{lang}{i}"})
        statuses.append(status)
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
    return {"lang": lang, "client_p50": pct(client, 50), "client_p99": pct(client, 99),
            "total_p50": pct(total, 50), "redis_p50": pct(redis, 50), "backend_p50": pct(backend, 50)}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    endpoint = sys.argv[1].rstrip("/")
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    run_id = uuid.uuid4().hex[:6]
    c = Client(endpoint)

    # Router reachable? (Fails here if your IP isn't in the security group.)
    status, ms, _, _ = c.get("/healthz", {})
    print(f"router /healthz: {status} in {ms:.1f} ms")

    results, all_ok = [], True
    for lang in LANGS:
        print(f"\n== {lang}")
        all_ok &= limit_check(c, lang, run_id)
        # Let the shared ip bucket refill (size 20 at 10/s -> 2 s) before timing.
        time.sleep(2.5)
        results.append(latency_run(c, lang, n, run_id))
        time.sleep(2.5)

    # Markdown rows for the README latency table.
    print("\n| Language | client p50 | client p99 | server total p50 | redis p50 | backend p50 |")
    print("|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['lang']} | {fmt(r['client_p50'])} | {fmt(r['client_p99'])} | "
              f"{fmt(r['total_p50'])} | {fmt(r['redis_p50'])} | {fmt(r['backend_p50'])} |")
    print("\nAll limit checks OK" if all_ok else "\nSome limit checks were UNEXPECTED")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
