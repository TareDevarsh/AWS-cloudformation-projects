"""Leaky bucket (queue) limiter server, Python version.

This server is the "door": it decides who may join the line and answers polls.
It does NOT control the speed; the worker (worker.py) starts jobs at a fixed
rate. Example domain: a chat agent where every message waits for its turn.

Routes (nginx sends /python/<path> here; the prefix is stripped):
  POST /chat              header X-User-Id, optional JSON {"message": "..."}
                          202 {job_id, position, tier}  or  429 + Retry-After
  GET  /result/<job_id>   header X-User-Id (must be the job's owner, else 404)
                          200 {status: queued|processing|done|failed|expired,
                               position, answer?, queue_wait_ms?, processing_ms?}
                          429 + Retry-After if the client polls too fast
  GET  /healthz           200, no checks

Request flow of POST /chat:
  1. Validate the user id and the body.
  2. One Redis call runs accept.lua: tier, room in the user's line?, ticket.
  3. Full -> 429. Room -> send the job to the SQS FIFO queue of the tier -> 202.

Every response carries a Server-Timing header, so the laptop can see where
the time went:  Server-Timing: redis;dur=0.42, sqs;dur=14.2, total;dur=15.0

Config: see common.py, plus
  PATH_PREFIX        prefix nginx routes on (default /python)
  HOST / PORT        listen address (default 127.0.0.1:8000)
  TOKEN_BUCKET_LUA   path to the EXISTING token bucket script, reused for the
                     polling limit (not copied)
"""

# --- Imports ----------------------------------------------------------------

import asyncio  # async event loop: one process handles many requests at once
import json  # builds and reads JSON bodies
import os  # reads environment variables (config)
import time  # perf_counter() for the latency timings
import uuid  # random job ids

import boto3  # AWS SDK: SQS (send jobs to the line)
import redis.asyncio as aioredis  # async Redis client
from botocore.config import Config  # short timeouts for SQS calls

# Shared helpers: config, rules, names.
from common import (ACCEPT_LUA, CALLER_ID, FINISH_LUA, HERE, JOB_ID, JOB_TTL_S, LANGUAGE,
                    LINE_TTL_CUSHION_S, REDIS_URL, Rules, job_key, log, queue_url, read_text)

# --- Config (from environment variables, with local defaults) ---------------

# The EXISTING token bucket script, reused as-is for the polling limit.
TOKEN_BUCKET_LUA = os.environ.get("TOKEN_BUCKET_LUA",
                                  os.path.join(HERE, "..", "..", "token_bucket", "token_bucket.lua"))
# URL prefix nginx uses for this language; stripped before routing.
PATH_PREFIX = os.environ.get("PATH_PREFIX", "/python").rstrip("/")
# Longest chat message we accept, in characters.
MAX_MESSAGE_CHARS = 500
# Largest request body we read, in bytes (a 500 character message fits many times over).
MAX_BODY_BYTES = 4096
# Every job of a queue uses the same group id, so SQS keeps strict first-in, first-out order.
GROUP_ID = "q"

# --- App (plain ASGI, no framework, to keep per-request overhead small) ------


class App:
    def __init__(self, redis_client=None, sqs_client=None):
        # Redis connection pool (connects on first use). Tests can pass a fake client.
        # decode_responses=True gives text instead of bytes.
        self.redis = redis_client or aioredis.from_url(REDIS_URL, decode_responses=True)
        # register_script sends EVALSHA and reloads the script if Redis lost it.
        self.accept = self.redis.register_script(read_text(ACCEPT_LUA))
        # Ends or advances a job (used here only to give back a place on errors / expiry).
        self.finish = self.redis.register_script(read_text(FINISH_LUA))
        # The token bucket script from the other algorithm, used for the polling limit.
        self.poll_limit = self.redis.register_script(read_text(TOKEN_BUCKET_LUA))
        # SQS client: signs calls with the instance role. Short timeouts so a slow
        # SQS cannot hold requests for long. Tests can pass a client of their own.
        self.sqs = sqs_client or boto3.client("sqs", config=Config(connect_timeout=2, read_timeout=5))
        # In-memory rules (loaded in startup()).
        self.rules = Rules()

    async def startup(self):
        # Load rules before taking traffic (boto3/file IO in a worker thread).
        await asyncio.to_thread(self.rules.load_initial)
        # TEST ONLY: write the seed users into the tiers hash.
        await self.seed_tiers()
        # With S3, keep checking for rule changes in the background.
        if self.rules.s3:
            self._refresh_task = asyncio.create_task(self.rules.refresh_forever(self.seed_tiers))

    async def seed_tiers(self):
        # Seed users from rules.yaml (test only). HSET is safe to repeat.
        seed = self.rules.cfg["seed"]
        if seed:
            # One call writes all users: field = user id, value = tier.
            await self.redis.hset("tiers", mapping=seed)
            log.info("seeded %d test users into the tiers hash", len(seed))

    async def __call__(self, scope, receive, send):
        # uvicorn calls this for startup/shutdown events and for each request.
        if scope["type"] == "lifespan":
            # Lifespan: wait for startup/shutdown messages.
            while True:
                msg = await receive()
                # Server starting: load rules, then say we're ready.
                if msg["type"] == "lifespan.startup":
                    await self.startup()
                    await send({"type": "lifespan.startup.complete"})
                # Server stopping: close Redis connections cleanly.
                elif msg["type"] == "lifespan.shutdown":
                    await self.redis.aclose()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        # Ignore anything that isn't plain HTTP (e.g. websockets).
        if scope["type"] != "http":
            return
        # Handle the HTTP request.
        await self.handle(scope, receive, send)

    async def handle(self, scope, receive, send):
        # Start the clock for this request.
        t0 = time.perf_counter()
        # Full path from nginx, e.g. "/python/chat".
        path = scope["path"]
        # Strip the language prefix: "/python/chat" -> "/chat".
        if path.startswith(PATH_PREFIX):
            path = path[len(PATH_PREFIX):] or "/"
        # HTTP method, e.g. "POST".
        method = scope["method"]

        # Health check: no user id, no Redis, no SQS.
        if path == "/healthz":
            return await respond(send, 200, b'{"ok":true}', [])

        # Which route is this? Anything else is a 404.
        is_chat = path == "/chat"
        is_result = path.startswith("/result/")
        if not (is_chat or is_result):
            return await respond(send, 404, error_body("not found"), [server_timing([("total", time.perf_counter() - t0)])])
        # Each route allows exactly one method.
        if method != ("POST" if is_chat else "GET"):
            return await respond(send, 405, error_body("method not allowed"),
                                 [server_timing([("total", time.perf_counter() - t0)])])

        # The caller must say who they are (test only: callers can fake this header).
        user = header(scope, "x-user-id")
        # Missing or unsafe characters -> 400.
        if not user or not CALLER_ID.match(user):
            return await respond(send, 400, error_body("X-User-Id header is required (letters, digits, _ . :)"),
                                 [server_timing([("total", time.perf_counter() - t0)])])

        # Hand over to the route.
        if is_chat:
            return await self.chat(scope, receive, send, user, t0)
        return await self.result(send, user, path[len("/result/"):], t0)

    # --- POST /chat ---------------------------------------------------------

    async def chat(self, scope, receive, send, user, t0):
        # 1. Read and check the body (memory only, no network).
        body = await read_body(receive)
        # Too big: stop before parsing.
        if body is None:
            return await respond(send, 413, error_body("body too large"),
                                 [server_timing([("total", time.perf_counter() - t0)])])
        # No body is fine: an empty message.
        message, fail = "", False
        if body:
            try:
                # Must be a JSON object, e.g. {"message": "hi"}.
                data = json.loads(body)
                message = data.get("message", "")
                # Test hook: {"fail": true} makes the backend raise, to test retries.
                fail = data.get("fail") is True
                # The message must be text and not too long.
                if not isinstance(message, str) or len(message) > MAX_MESSAGE_CHARS:
                    raise ValueError("message must be text of at most %d characters" % MAX_MESSAGE_CHARS)
            # Not JSON, not an object, or a bad message: 400.
            except (ValueError, AttributeError) as e:
                return await respond(send, 400, error_body(f"bad body: {e}"),
                                     [server_timing([("total", time.perf_counter() - t0)])])

        # Current settings (a plain dict, replaced as a whole when rules change).
        cfg = self.rules.cfg
        # A random, hard to guess id for this job.
        job_id = str(uuid.uuid4())
        # Lua args: language, user, job id, line count lifetime, job lifetime,
        # then 3 numbers per tier (tier, max_waiting, interval_ms).
        args = [LANGUAGE, user, job_id, cfg["queue"]["retention_s"] + LINE_TTL_CUSHION_S, JOB_TTL_S]
        for t in cfg["tiers"].values():
            args += [t["id"], t["max_waiting"], int(round(t["interval_ms"]))]
        # Time after step 1.
        t1 = time.perf_counter()

        # 2. One Redis round trip: tier, room in line?, ticket, job record.
        accepted, tier, ticket, position, retry_ms = await self.accept(keys=["tiers"], args=args)
        # Time after step 2.
        t2 = time.perf_counter()

        # Line full: answer 429 without touching SQS.
        if not accepted:
            # Retry-After in whole seconds, rounded up, at least 1.
            headers = [(b"retry-after", str(max(1, -(-retry_ms // 1000))).encode())]
            timing = [("redis", t2 - t1), ("total", t2 - t0)]
            body = json.dumps({"error": "line full", "tier": tier, "retry_after_ms": retry_ms}).encode()
            return await respond(send, 429, body, headers + [server_timing(timing)])

        # 3. Joined the line: put the job in this tier's SQS FIFO queue.
        message_body = json.dumps({"job": job_id, "msg": message, "fail": fail})
        try:
            # boto3 is blocking, so run it in a worker thread.
            await asyncio.to_thread(
                self.sqs.send_message, QueueUrl=queue_url(tier), MessageBody=message_body,
                MessageGroupId=GROUP_ID, MessageDeduplicationId=job_id)
        except Exception:
            # Could not queue it: give the place in line back, then tell the client.
            log.exception("SQS send failed for job %s", job_id)
            await self.finish(keys=[job_key(job_id)], args=["failed", "could not queue the job", JOB_TTL_S])
            return await respond(send, 502, error_body("queue unavailable"),
                                 [server_timing([("redis", t2 - t1), ("total", time.perf_counter() - t0)])])
        # Time after step 3.
        t3 = time.perf_counter()

        # Tell the client where it stands; it polls GET /result/<job_id> from here.
        body = json.dumps({"job_id": job_id, "position": position, "tier": tier}).encode()
        timing = [("redis", t2 - t1), ("sqs", t3 - t2), ("total", t3 - t0)]
        return await respond(send, 202, body, [server_timing(timing)])

    # --- GET /result/<job_id> -----------------------------------------------

    async def result(self, send, user, job_id, t0):
        # Not even shaped like one of our ids: 404, and Redis is not touched.
        if not JOB_ID.match(job_id):
            return await respond(send, 404, error_body("no such job"), [server_timing([("total", time.perf_counter() - t0)])])

        # Current settings.
        cfg = self.rules.cfg
        # 1. Polling limit: reuse the token bucket script on two buckets (all-or-nothing):
        #    one per job and one per user. The job bucket also covers unknown ids.
        (jsize, jrate), (usize, urate) = cfg["poll"]["job"], cfg["poll"]["user"]
        allowed, _, retry_ms, _ = await self.poll_limit(
            keys=[f"poll-u{job_id}", f"pollu-u{user}"], args=[jsize, jrate, 1, usize, urate, 1])
        # Time after step 1.
        t1 = time.perf_counter()
        # Too many polls: 429 with a wait time.
        if not allowed:
            headers = [(b"retry-after", str(max(1, -(-retry_ms // 1000))).encode())]
            timing = [("redis", t1 - t0), ("total", t1 - t0)]
            body = json.dumps({"error": "polling too fast", "retry_after_ms": retry_ms}).encode()
            return await respond(send, 429, body, headers + [server_timing(timing)])

        # 2. Read the job, the Redis clock, and every tier's "now serving" in one round trip.
        job, now_ms, serving = await self.read_job(job_id)
        # Unknown job (never existed or its record expired) or someone else's job: 404.
        if not job or job.get("u") != user:
            return await respond(send, 404, error_body("no such job"),
                                 [server_timing([("redis", time.perf_counter() - t1), ("total", time.perf_counter() - t0)])])

        # 3. A job that is still waiting but older than the SQS retention has lost its
        #    message (SQS dropped it): end it as "expired" and give the place back.
        if job["st"] in ("queued", "processing") and now_ms - int(job["t0"]) > cfg["queue"]["retention_s"] * 1000:
            await self.finish(keys=[job_key(job_id)], args=["expired", "expired in the queue before it was processed", JOB_TTL_S])
            # Read again: if a worker finished it first, we report what really happened.
            job, now_ms, serving = await self.read_job(job_id)
        # Time after step 2/3.
        t2 = time.perf_counter()

        # Build the answer. Status and place in line (0 once the job has ended).
        status = job["st"]
        out = {"status": status, "tier": int(job["tr"]), "position": 0}
        if status in ("queued", "processing"):
            # Place in line = ticket - now serving of its tier (at least 1).
            out["position"] = max(1, int(job["tk"]) - serving.get(int(job["tr"]), 0))
        else:
            # Ended: how long it waited and how long the agent took.
            out["queue_wait_ms"], out["processing_ms"] = int(job.get("qw", 0)), int(job.get("pm", 0))
            # The agent's answer, or why it failed.
            out["answer" if status == "done" else "error"] = job.get("a", "")
        timing = [("redis", t2 - t1), ("total", t2 - t0)]
        return await respond(send, 200, json.dumps(out).encode(), [server_timing(timing)])

    async def read_job(self, job_id):
        """Job record, Redis time (ms) and each tier's 'now serving', in one round trip."""
        # Pipeline = send all commands together, get all answers together.
        pipe = self.redis.pipeline(transaction=False)
        # The job record (empty dict if missing).
        pipe.hgetall(job_key(job_id))
        # Redis server clock: {seconds, microseconds}.
        pipe.time()
        # "Now serving" of every configured tier (we don't know the job's tier yet).
        tier_ids = list(self.rules.cfg["tiers"])
        for tid in tier_ids:
            pipe.get(f"sv-{LANGUAGE}-{tid}")
        # Send, then unpack.
        res = await pipe.execute()
        # Milliseconds since 1970 from the Redis clock.
        now_ms = int(res[1][0]) * 1000 + int(res[1][1]) // 1000
        # tier id -> last finished ticket (0 if never set).
        serving = {tid: int(v or 0) for tid, v in zip(tier_ids, res[2:])}
        return res[0], now_ms, serving


# --- Small helpers ----------------------------------------------------------


def header(scope, name):
    """First value of a request header (ASGI headers are lowercase bytes)."""
    # ASGI gives header names as lowercase bytes, so convert ours to match.
    name = name.lower().encode()
    # Headers are a list of (name, value) pairs.
    for k, v in scope["headers"]:
        # Found it: return the value as text.
        if k == name:
            return v.decode("latin-1")
    # Header not sent.
    return None


async def read_body(receive):
    """Request body as bytes, or None if it is larger than MAX_BODY_BYTES."""
    # The body can arrive in several pieces.
    chunks, size = [], 0
    while True:
        # Next piece from the server.
        msg = await receive()
        # Not a body piece (e.g. client disconnected): stop with what we have.
        if msg["type"] != "http.request":
            break
        # Count the bytes and refuse a body that is too large.
        chunk = msg.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
        # The last piece has more_body False.
        if not msg.get("more_body", False):
            break
    # All pieces joined.
    return b"".join(chunks)


def error_body(text):
    """Small JSON error body."""
    # {"error": "..."} as bytes.
    return json.dumps({"error": text}).encode()


def server_timing(parts):
    """Standard Server-Timing header, durations in ms."""
    # e.g. "redis;dur=0.410, sqs;dur=14.200, total;dur=15.000"
    return (b"server-timing", ", ".join(f"{n};dur={d * 1000:.3f}" for n, d in parts).encode())


async def respond(send, status, body, headers):
    # Send status line and headers (JSON body, exact length).
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())] + headers})
    # Send the body; the response is complete.
    await send({"type": "http.response.body", "body": body})


# The ASGI app uvicorn serves.
app = App()

# Run directly with "python app.py".
if __name__ == "__main__":
    # Imported here so tests can import this file without uvicorn starting.
    import uvicorn

    # Default is loopback. The Dockerfile sets HOST=0.0.0.0 because inside the
    # container only the Docker network can reach it (no port is published).
    # access_log=False: no log line per request, to keep latency low.
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")), access_log=False)
