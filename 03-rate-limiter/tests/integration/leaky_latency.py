"""Live test for the deployed leaky bucket (queue) limiter: correctness + latency per language.

Usage:
    python3 tests/integration/leaky_latency.py http://<ec2-ip>:8080 [rounds] [lang,lang,...]

No installs needed (standard library only). Run it from the laptop whose IP
you allowed in the security group. The stack must be deployed with the
leaky_bucket algorithm (see the project README).

What it does, for each language (python, go, node), one at a time. The client
polls GET /<lang>/result/<job_id> once per second, like a real chat client.
  a) Regular user (random id, tier 0: 1 job/s, 3 waiting): a burst of 5 POSTs
     sent in parallel -> expect 3 x 202 and 2 x 429. The 3 accepted jobs end
     "done", started about 0, 1 and 2 s after the burst (fixed rate).
  b) Business user (seeded user biz1..biz5, tier 1: 5 jobs/s, 10 waiting):
     a burst of 5 in parallel -> expect 5 x 202, all done within ~1.5 s.
  c) Poll limit: 8 polls of one job at the same moment -> expect some 429
     with a Retry-After header (the allowance is 3 in a burst, then 1/s).
  d) Failure: a job sent with {"fail": true} (test hook, travels to the
     backend) -> expect status "failed" after 5 attempts, and the user's line
     slot given back (a following request is accepted).
  (a) and (b) are repeated `rounds` times (default 3) for more samples.

Prints p50 and p90 (ms) of:
  accept       POST round trip seen here (client) and inside the limiter (server)
  queue wait   time in line until the worker started the job (from the job result)
  processing   worker start -> the agent's answer (from the job result)
  end-to-end   POST sent -> first poll that shows "done" (includes up to 1 s of poll delay)
then a markdown table for the README. Exits 1 if anything was unexpected.
"""
import http.client  # keep-alive HTTP connection (no new TCP handshake per request)
import json  # build request bodies and parse response bodies
import statistics  # quantiles for p50/p90
import sys  # command-line arguments and exit code
import threading  # barrier so a burst really starts at the same moment
import time  # timing, pacing and polling
import urllib.parse  # split the endpoint URL
import uuid  # unique user ids per run
from concurrent.futures import ThreadPoolExecutor  # parallel bursts and pollers

LANGS = ["python", "go", "node"]  # the three limiter+worker pairs behind the router
BUSINESS_USERS = ["biz1", "biz2", "biz3", "biz4", "biz5"]  # seeded into tier 1 by rules.yaml (test only)
POLL_EVERY_S = 1.0  # the client polls once per second (the project requirement)
POLL_TIMEOUT_S = 30.0  # give up on a normal job after this long (regular worst case is ~3 s)
FAIL_TIMEOUT_S = 60.0  # a failing job takes ~6 s (5 attempts); allow plenty of margin
BURST = 5  # requests sent at once in scenarios (a) and (b)
TERMINAL = {"done", "failed", "expired"}  # job statuses that never change again


def parse_timing(header):
    """'redis;dur=0.4, sqs;dur=12.0' -> {'redis': 0.4, 'sqs': 12.0} (ms)."""
    out = {}  # name -> duration in ms
    for part in (header or "").split(","):  # one entry per comma
        name, _, dur = part.strip().partition(";dur=")  # split 'name;dur=1.2'
        if name and dur:  # ignore empty or malformed parts
            out[name] = float(dur)  # keep the duration as a number
    return out  # may be empty


class Client:
    """One persistent HTTP connection to the router (one per thread, never shared)."""

    def __init__(self, endpoint):
        u = urllib.parse.urlparse(endpoint)  # split scheme, host and port
        self.host, self.port = u.hostname, u.port or 80  # port 80 if none given
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=15)  # not connected yet

    def connect(self):
        """Open the TCP connection now, so a burst does not include the handshake."""
        self.conn.connect()  # raises OSError if the router is unreachable

    def call(self, method, path, headers, body=None):
        """Send one request; return (status, client_ms, server_timing, retry_after, parsed_json)."""
        t0 = time.perf_counter()  # start the client-side clock
        try:
            self.conn.request(method, path, body=body, headers=headers)  # send the request
            resp = self.conn.getresponse()  # wait for the status line and headers
            raw = resp.read()  # read the whole body (keeps the connection reusable)
        except (http.client.HTTPException, OSError):
            # Connection dropped (idle timeout, reset): reconnect once and retry.
            self.conn.close()  # drop the dead socket
            self.conn = http.client.HTTPConnection(self.host, self.port, timeout=15)  # fresh connection
            self.conn.request(method, path, body=body, headers=headers)  # send again
            resp = self.conn.getresponse()  # wait for the answer
            raw = resp.read()  # read the body
        ms = (time.perf_counter() - t0) * 1000  # client-side round trip in ms
        try:
            data = json.loads(raw) if raw else {}  # bodies are JSON
        except ValueError:
            data = {}  # not JSON (e.g. an nginx error page): treat as empty
        retry = resp.getheader("Retry-After")  # only set on 429
        return resp.status, ms, parse_timing(resp.getheader("Server-Timing")), retry, data

    def chat(self, lang, user, message="hello", fail=False):
        """POST /<lang>/chat. `fail` is the test hook: the backend raises for this job."""
        body = {"message": message}  # the chat message
        if fail:  # only send the hook when asked, so normal calls look normal
            body["fail"] = True  # the limiter puts it in the SQS message body for the worker
        headers = {"X-User-Id": user, "Content-Type": "application/json"}  # who is asking
        return self.call("POST", f"/{lang}/chat", headers, json.dumps(body))  # (status, ms, timing, retry, data)

    def result(self, lang, user, job_id):
        """GET /<lang>/result/<job_id> as the job's owner."""
        return self.call("GET", f"/{lang}/result/{job_id}", {"X-User-Id": user})  # (status, ms, timing, retry, data)


def pct(values, p):
    """p-th percentile (p in 1..99) of a list, or None if empty."""
    if len(values) < 2:  # quantiles need two points
        return values[0] if values else None  # one value is its own percentile
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]  # inclusive = stays inside the data


def fmt(v):
    """Number with two decimals, or a dash when there is no data."""
    return "   -   " if v is None else f"{v:7.2f}"  # fixed width so columns line up


def fmt_row(v):
    """Same, but without padding (for the markdown table)."""
    return "-" if v is None else f"{v:.0f}"  # whole milliseconds are enough in the table


class Run:
    """Collects checks (pass/fail lines) and the timing samples of the whole run."""

    def __init__(self):
        self.ok = True  # turns False at the first unexpected result
        self.samples = {}  # (lang, tier) -> {'accept': [], 'accept_srv': [], 'queue': [], 'proc': [], 'e2e': []}

    def check(self, label, good, detail=""):
        """Print one line and remember a failure."""
        print(f"  [{'ok' if good else 'UNEXPECTED'}] {label}" + (f"  ({detail})" if detail else ""))  # one line per check
        self.ok = self.ok and good  # a single failure fails the run

    def add(self, lang, tier, **vals):
        """Add timing samples for one (language, tier) pair."""
        s = self.samples.setdefault((lang, tier), {"accept": [], "accept_srv": [], "queue": [], "proc": [], "e2e": []})  # create on first use
        for k, v in vals.items():  # e.g. accept=12.3
            s[k].append(v)  # keep every sample


def poll_until_done(endpoint, lang, user, job_id, t_sent, timeout_s):
    """Poll one job every second until it ends. Returns a dict with the last body and timings."""
    c = Client(endpoint)  # own connection for this job
    start = time.perf_counter()  # poll schedule starts now, so polls are 1.0 s apart without drift
    n, limited, last, done_at = 0, 0, {}, None  # poll count, 429 count, last body, time of the end
    while True:
        status, _, _, retry, data = c.result(lang, user, job_id)  # one poll
        n += 1  # count polls
        if status == 429:  # the poll limit hit: honour Retry-After, and report it as unexpected
            limited += 1  # counted
            time.sleep(float(retry or 1))  # wait as the server asked
        elif status == 200:  # normal answer
            last = data  # remember the newest body
            if data.get("status") in TERMINAL:  # done, failed or expired
                done_at = time.perf_counter()  # when the client learned about it
                break  # stop polling
        else:  # 404, 5xx, ...: not a state a job can be in
            last = {"status": f"http {status}", "error": str(data)}  # show it in the report
            break  # nothing more to wait for
        if time.perf_counter() - start > timeout_s:  # gave up waiting
            last = dict(last, status="timeout")  # mark as a timeout
            break  # stop polling
        time.sleep(max(0.0, start + n * POLL_EVERY_S - time.perf_counter()))  # next poll on the 1 s grid
    return {"body": last, "polls": n, "limited": limited, "end_ms": (done_at - t_sent) * 1000 if done_at else None}


def burst(endpoint, lang, users, count, message):
    """Send `count` POSTs at the same moment (one thread + connection each). Returns (t_sent, results)."""
    clients = [Client(endpoint) for _ in range(count)]  # one connection per request
    for c in clients:  # connect first, so only the request itself is timed
        c.connect()  # TCP handshake now
    gate = threading.Barrier(count)  # all threads wait here, then go together

    def one(i):
        gate.wait()  # released at the same moment for every thread
        return clients[i].chat(lang, users[i % len(users)], message=f"{message} {i}")  # the POST

    t_sent = time.perf_counter()  # burst start (the end-to-end clock)
    with ThreadPoolExecutor(max_workers=count) as pool:  # one thread per request, so they really overlap
        results = list(pool.map(one, range(count)))  # keeps request order
    return t_sent, results


def track(endpoint, lang, user, accepted, t_sent, timeout_s=POLL_TIMEOUT_S):
    """Poll every accepted job in parallel (the user has several jobs waiting at once)."""
    if not accepted:  # nothing to follow
        return []  # no jobs, no results
    with ThreadPoolExecutor(max_workers=len(accepted)) as pool:  # one poller per job
        futs = [pool.submit(poll_until_done, endpoint, lang, user, d["job_id"], t_sent, timeout_s) for d in accepted]  # start all
        return [f.result() for f in futs]  # wait for all, same order


def scenario_regular(run, endpoint, lang, round_no):
    """(a) Regular user: burst of 5 -> 3 accepted + 2 refused; done at ~0, 1, 2 s."""
    user = f"reg{uuid.uuid4().hex[:10]}"  # fresh random user, tier 0 (not in the tiers hash)
    print(f"  (a) regular user {user}, burst of {BURST} (round {round_no})")  # header line
    t_sent, res = burst(endpoint, lang, [user], BURST, "regular burst")  # 5 POSTs at once
    codes = sorted(r[0] for r in res)  # status codes only
    acc = [(r[1], r[2], r[4]) for r in res if r[0] == 202]  # (client ms, timing, body) of accepted
    run.check("3 x 202 and 2 x 429", codes == [202] * 3 + [429] * 2, f"got {codes}")  # the line holds 3
    run.check("429 carries Retry-After", any(r[0] == 429 for r in res) and all(r[3] for r in res if r[0] == 429), "")  # tells the client when to retry
    run.check("accepted jobs are tier 0 with positions 1,2,3",
              sorted((a[2].get("tier"), a[2].get("position")) for a in acc) == [(0, 1), (0, 2), (0, 3)],
              str(sorted((a[2].get("tier"), a[2].get("position")) for a in acc)))  # ticket - now serving
    outs = track(endpoint, lang, user, [a[2] for a in acc], t_sent)  # poll each job every second
    bodies = [o["body"] for o in outs]  # last answer per job
    run.check("all accepted jobs are done", bool(bodies) and all(b.get("status") == "done" for b in bodies),
              str([b.get("status") for b in bodies]))  # none failed or expired
    run.check("polling every 1 s was never limited", all(o["limited"] == 0 for o in outs), "")  # 1 poll/s fits the limit
    waits = sorted(b.get("queue_wait_ms", -1) for b in bodies)  # server-measured wait in line
    want = [0, 1000, 2000]  # fixed rate of 1 job/s
    run.check("started about 0, 1 and 2 s after the burst (+-600 ms)",
              len(waits) == 3 and all(abs(w - x) <= 600 for w, x in zip(waits, want)), f"queue_wait_ms {waits}")  # spacing
    ends = sorted(round(o["end_ms"] / 1000, 2) for o in outs if o["end_ms"] is not None)  # what the client saw
    print(f"      client saw done at {ends} s after the burst (includes up to 1 s poll delay)")  # info only
    for (ms, tm, _), o in zip(acc, outs):  # one sample set per accepted job
        b = o["body"]  # its result
        if b.get("status") == "done" and o["end_ms"] is not None:  # only finished jobs have timings
            run.add(lang, 0, accept=ms, accept_srv=tm.get("total", 0), queue=b["queue_wait_ms"],
                    proc=b["processing_ms"], e2e=o["end_ms"])  # store for the percentiles


def scenario_business(run, endpoint, lang, user, round_no):
    """(b) Business user: burst of 5 -> all accepted, all done within ~1.5 s."""
    print(f"  (b) business user {user}, burst of {BURST} (round {round_no})")  # header line
    t_sent, res = burst(endpoint, lang, [user], BURST, "business burst")  # 5 POSTs at once
    codes = sorted(r[0] for r in res)  # status codes only
    acc = [(r[1], r[2], r[4]) for r in res if r[0] == 202]  # accepted ones
    run.check("5 x 202", codes == [202] * BURST, f"got {codes}")  # line holds 10
    run.check("all accepted jobs are tier 1", bool(acc) and all(a[2].get("tier") == 1 for a in acc),
              str([a[2].get("tier") for a in acc]))  # the seeded user must map to tier 1
    outs = track(endpoint, lang, user, [a[2] for a in acc], t_sent)  # poll each job every second
    bodies = [o["body"] for o in outs]  # last answer per job
    run.check("all accepted jobs are done", bool(bodies) and all(b.get("status") == "done" for b in bodies),
              str([b.get("status") for b in bodies]))  # none failed or expired
    run.check("polling every 1 s was never limited", all(o["limited"] == 0 for o in outs), "")  # 5 jobs = 5 polls/s, under 10/s
    totals = [b.get("queue_wait_ms", 0) + b.get("processing_ms", 0) for b in bodies]  # time in line + agent
    run.check("all done within ~1.5 s (server view)", bool(totals) and max(totals) <= 1500, f"max {max(totals or [0])} ms")  # 5/s pace
    for (ms, tm, _), o in zip(acc, outs):  # one sample set per accepted job
        b = o["body"]  # its result
        if b.get("status") == "done" and o["end_ms"] is not None:  # only finished jobs have timings
            run.add(lang, 1, accept=ms, accept_srv=tm.get("total", 0), queue=b["queue_wait_ms"],
                    proc=b["processing_ms"], e2e=o["end_ms"])  # store for the percentiles


def scenario_poll_limit(run, endpoint, lang):
    """(c) Poll limit: 8 polls of one job at the same moment -> some 429."""
    user = f"pol{uuid.uuid4().hex[:10]}"  # fresh regular user
    print(f"  (c) poll limit, user {user}")  # header line
    c = Client(endpoint)  # one client for the POST
    status, _, _, _, data = c.chat(lang, user, "poll limit")  # one job to poll
    run.check("job accepted", status == 202, f"got {status}")  # needed for the rest
    if status != 202:  # nothing to poll
        return  # the failed check already marks the run
    clients = [Client(endpoint) for _ in range(8)]  # 8 connections, so the polls overlap
    for cl in clients:  # connect first
        cl.connect()  # TCP handshake now
    gate = threading.Barrier(8)  # release all polls together

    def one(i):
        gate.wait()  # same moment for everyone
        return clients[i].result(lang, user, data["job_id"])  # one poll

    with ThreadPoolExecutor(max_workers=8) as pool:  # parallel polls
        res = list(pool.map(one, range(8)))  # (status, ms, timing, retry, body)
    codes = sorted(r[0] for r in res)  # status codes only
    limited = [r for r in res if r[0] == 429]  # the refused polls
    run.check("some polls got 429", len(limited) >= 1, f"got {codes}")  # the bucket holds 3, so about 5 are refused
    run.check("429 carries Retry-After", bool(limited) and all(r[3] for r in limited), "")  # tells the client when to retry
    run.check("429 body names the reason", bool(limited) and all(r[4].get("error") == "polling too fast" for r in limited), "")  # clear message
    track(endpoint, lang, user, [data], time.perf_counter())  # let the job finish so the user's line is empty again


def scenario_failure(run, endpoint, lang):
    """(d) Failing job -> `failed` after 5 attempts, and the line slot is given back."""
    user = f"bad{uuid.uuid4().hex[:10]}"  # fresh regular user
    print(f"  (d) failing job, user {user} (takes ~6 s: 5 attempts, 1 s apart)")  # header line
    c = Client(endpoint)  # client for the POSTs
    t_sent = time.perf_counter()  # clock for the whole failure
    status, _, _, _, data = c.chat(lang, user, "please fail", fail=True)  # the test hook
    run.check("failing job accepted", status == 202, f"got {status}")  # accepted like any other
    if status != 202:  # nothing to follow
        return  # the failed check already marks the run
    out = poll_until_done(endpoint, lang, user, data["job_id"], t_sent, FAIL_TIMEOUT_S)  # wait for the end
    body = out["body"]  # last answer
    run.check("status is failed", body.get("status") == "failed", str(body.get("status")))  # not done, not expired
    run.check("error mentions 5 attempts", "5 attempts" in str(body.get("error", "")), str(body.get("error", ""))[:80])  # max_attempts
    print(f"      failed after {out['end_ms'] / 1000:.1f} s" if out["end_ms"] else "      no end seen")  # info only
    status2, _, _, _, data2 = c.chat(lang, user, "after the failure")  # the slot must be free again
    run.check("line slot given back (next request accepted)", status2 == 202, f"got {status2}")  # lq- count went down
    if status2 == 202:  # finish the follow-up job so nothing is left waiting
        track(endpoint, lang, user, [data2], time.perf_counter())  # poll it to the end


def report(run, langs):
    """Print p50/p90 per (language, tier) and a markdown table."""
    tiers = {0: "regular", 1: "business"}  # tier number -> name (matches rules.yaml)
    rows = []  # table rows, filled below
    for lang in langs:  # same order as the run
        for tier in (0, 1):  # regular first
            s = run.samples.get((lang, tier))  # samples of this pair
            if not s:  # nothing measured (e.g. the scenario failed)
                continue  # skip the row
            print(f"\n  {lang} / {tiers[tier]}  ({len(s['e2e'])} jobs)      p50      p90   (ms)")  # block header
            for name, key in [("accept (client)", "accept"), ("accept (server)", "accept_srv"), ("queue wait", "queue"),
                              ("processing", "proc"), ("end-to-end", "e2e")]:  # one line per timing
                print(f"    {name:16} {fmt(pct(s[key], 50))}  {fmt(pct(s[key], 90))}")  # p50 and p90
            rows.append((lang, tiers[tier], len(s["e2e"]), s))  # remember for the table
    print("\n| Language | Tier | Jobs | accept client p50 / p90 | accept server p50 | queue wait p50 / p90 | processing p50 / p90 | end-to-end p50 / p90 |")  # table header
    print("|---|---|---|---|---|---|---|---|")  # table separator
    for lang, name, n, s in rows:  # one row per pair
        two = lambda k: f"{fmt_row(pct(s[k], 50))} / {fmt_row(pct(s[k], 90))}"  # 'p50 / p90' text
        print(f"| {lang} | {name} | {n} | {two('accept')} | {fmt_row(pct(s['accept_srv'], 50))} | "
              f"{two('queue')} | {two('proc')} | {two('e2e')} |")  # one row
    print("\nAll values in ms. Queue wait is by design (it grows with the line); end-to-end includes up to 1 s of poll delay.")  # reading hint


def main():
    if len(sys.argv) < 2:  # endpoint is required
        print(__doc__)  # show the usage text
        sys.exit(2)  # usage error
    endpoint = sys.argv[1].rstrip("/")  # e.g. http://203.0.113.10:8080
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3  # how often (a) and (b) repeat
    langs = sys.argv[3].split(",") if len(sys.argv) > 3 else LANGS  # optional subset, e.g. "python"
    bad = [l for l in langs if l not in LANGS]  # only the three known languages
    if bad or rounds < 1:  # reject a typo early
        print(f"unknown language {bad} or rounds < 1; languages: {LANGS}")  # explain
        sys.exit(2)  # usage error
    run = Run()  # collects checks and samples
    c = Client(endpoint)  # for the first reachability check
    try:
        status, ms, _, _, _ = c.call("GET", "/healthz", {})  # router reachable? (fails if your IP is not allowed)
    except OSError as e:
        print(f"router not reachable: {e}")  # most often: the security group has no rule for your IP
        sys.exit(1)  # cannot test anything
    print(f"router /healthz: {status} in {ms:.1f} ms")  # reachable
    for li, lang in enumerate(langs):  # one language at a time, so they do not compete for CPU
        print(f"\n== {lang}")  # section header
        for r in range(1, rounds + 1):  # repeat (a) and (b) for more samples
            scenario_regular(run, endpoint, lang, r)  # (a)
            time.sleep(2.0)  # let the regular lane go idle (no leftover pacing)
            scenario_business(run, endpoint, lang, BUSINESS_USERS[(li * rounds + r - 1) % len(BUSINESS_USERS)], r)  # (b)
            time.sleep(1.0)  # let the business lane go idle
        scenario_poll_limit(run, endpoint, lang)  # (c)
        scenario_failure(run, endpoint, lang)  # (d)
        time.sleep(2.0)  # settle before the next language
    report(run, langs)  # percentiles and the markdown table
    print("\nAll checks OK" if run.ok else "\nSome checks were UNEXPECTED")  # verdict
    sys.exit(0 if run.ok else 1)  # non-zero exit on anything unexpected


if __name__ == "__main__":  # run only when called as a script
    main()
