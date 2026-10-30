"""Sliding window log rate limiter server, Python version.

Request flow (same in every language):
  1. nginx sends /python/<path> here.
  2. Work out who the caller is: user id, session cookie, IP.
  3. Pick the matching rules (kept in memory) and build one Redis key each.
  4. One Redis call runs sliding_window_log.lua on all keys (all-or-nothing).
  5. Allowed -> invoke the backend Lambda. Denied -> 429.

Every response carries a Server-Timing header, so the laptop can see where
the time went:  Server-Timing: rules;dur=0.01, redis;dur=0.42, backend;dur=18.3, total;dur=18.9

Config (environment variables):
  REDIS_URL          redis://redis:6379/0
  LUA_PATH           path to sliding_window_log.lua
  RULES_FILE         local rules.yaml (used if RULES_BUCKET is empty)
  RULES_BUCKET       S3 bucket holding the rules (optional)
  RULES_KEY          S3 object key (default rules.yaml)
  RULES_REFRESH_S    seconds between S3 ETag checks (default 30)
  BACKEND_FUNCTION   Lambda function name; empty = built-in echo backend
  PATH_PREFIX        prefix nginx routes on (default /python)
  HOST / PORT        listen address (default 127.0.0.1:8000)
"""

# --- Imports ----------------------------------------------------------------

import asyncio  # async event loop: one process handles many requests at once
import fnmatch  # shell-style wildcard matching for rule paths ("/api/?")
import ipaddress  # parses IPv4/IPv6 text and turns IPv4 into a number
import json  # builds JSON request/response bodies
import logging  # startup and error logs (not per request, to keep it fast)
import os  # reads environment variables (config)
import re  # regular expressions to validate ids
import time  # perf_counter() for the latency timings
import uuid  # unique request id for the log entry
from http.cookies import CookieError, SimpleCookie  # parses the Cookie header

import boto3  # AWS SDK: S3 (rules) and Lambda (backend)
import redis.asyncio as aioredis  # async Redis client
import yaml  # reads rules.yaml
from botocore.exceptions import ClientError  # error type boto3 raises (e.g. S3 304)

# Logger named "limiter" so our lines are easy to find.
log = logging.getLogger("limiter")
# Log format: time, level, message.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --- Config (from environment variables, with local defaults) ---------------

# Where Redis is. In Docker Compose this is redis://redis:6379/0.
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
# The shared Lua algorithm; by default the file next to this folder.
LUA_PATH = os.environ.get("LUA_PATH", os.path.join(os.path.dirname(__file__), "..", "sliding_window_log.lua"))
# Local rules file, used when no S3 bucket is set (local testing).
RULES_FILE = os.environ.get("RULES_FILE", os.path.join(os.path.dirname(__file__), "..", "rules.yaml"))
# S3 bucket with the rules. Empty = use the local file.
RULES_BUCKET = os.environ.get("RULES_BUCKET", "")
# Name of the rules object inside the bucket.
RULES_KEY = os.environ.get("RULES_KEY", "rules.yaml")
# How often to ask S3 "did the rules change?".
RULES_REFRESH_S = float(os.environ.get("RULES_REFRESH_S", "30"))
# Backend Lambda name. Empty = echo the request back (local testing).
BACKEND_FUNCTION = os.environ.get("BACKEND_FUNCTION", "")
# URL prefix nginx uses for this language; stripped before matching rules.
PATH_PREFIX = os.environ.get("PATH_PREFIX", "/python").rstrip("/")

# Rule ids: lowercase letters and digits only, so "<id>-u<caller>" has one meaning.
RULE_ID = re.compile(r"^[a-z0-9]+$")
# Caller ids from headers/cookies: safe characters, max 128. Junk is ignored.
CALLER_ID = re.compile(r"^[A-Za-z0-9_.:]{1,128}$")
# The identity types a rule can use (see docs: "Identifying the caller").
VALID_KEYS = {"user_id", "cookie", "ip"}

# --- Rules (kept in memory) -------------------------------------------------


def parse_rules(text):
    """Validate rules.yaml text and return a list of rule dicts."""
    # safe_load never runs code from the file; empty file -> {}.
    data = yaml.safe_load(text) or {}
    # Collected, validated rules.
    rules = []
    # One entry per rule in the file.
    for r in data.get("rules", []):
        # Rule id as text, e.g. "user".
        rid = str(r["id"])
        # Reject ids that would make Redis keys ambiguous.
        if not RULE_ID.match(rid):
            raise ValueError(f"rule id must be [a-z0-9]+: {rid!r}")
        # Which identity this rule limits: user_id, cookie or ip.
        key = r["match"]["key"]
        # Reject unknown identity types.
        if key not in VALID_KEYS:
            raise ValueError(f"rule {rid}: match.key must be one of {sorted(VALID_KEYS)}")
        # Requests per window and window length as numbers.
        limit, window = float(r["limit"]), float(r["window_s"])
        # Limit: whole number >= 1; window: > 0 (the Lua script checks this too).
        if limit < 1 or limit != int(limit) or window <= 0:
            raise ValueError(f"rule {rid}: limit must be a whole number > 0 and window_s > 0")
        # Store a flat, ready-to-use version of the rule.
        rules.append({"id": rid, "key": key, "path": str(r["match"].get("path", "*")),
                      "limit": int(limit), "window": window})
    # Hand back the full list.
    return rules


class Rules:
    """Holds the current rules in memory and refreshes them from S3 by ETag."""

    def __init__(self):
        # Current rules; replaced as a whole on refresh.
        self.rules = []
        # S3 version tag of the rules we hold; None = nothing loaded yet.
        self.etag = None
        # S3 client only if a bucket is configured.
        self.s3 = boto3.client("s3") if RULES_BUCKET else None

    def load_initial(self):
        # Read rules once at startup.
        if self.s3:
            # From S3 (deployed).
            self._fetch_s3()
        else:
            # From the local file (testing).
            with open(RULES_FILE, encoding="utf-8") as f:
                self.rules = parse_rules(f.read())
        # Confirm in the log how many rules we have.
        log.info("loaded %d rules", len(self.rules))

    def _fetch_s3(self):
        """GET the rules only if changed. S3 answers 304 when the ETag matches."""
        # Which object to read.
        kwargs = {"Bucket": RULES_BUCKET, "Key": RULES_KEY}
        # If we already have a version, ask S3 to send the file only if it's different.
        if self.etag:
            kwargs["IfNoneMatch"] = self.etag
        try:
            # Read the object (or get a 304 "not modified").
            obj = self.s3.get_object(**kwargs)
        except ClientError as e:
            # 304 = unchanged: keep the rules we have.
            if e.response.get("Error", {}).get("Code") in ("304", "NotModified"):
                return False
            # Any other error goes to the caller.
            raise
        # Changed: parse and swap in the new rules in one assignment.
        self.rules = parse_rules(obj["Body"].read().decode("utf-8"))
        # Remember this version for the next check.
        self.etag = obj["ETag"]
        # Tell the caller the rules changed.
        return True

    async def refresh_forever(self):
        """Background task: check S3 every RULES_REFRESH_S. Keeps old rules on error."""
        # Runs for the life of the server.
        while True:
            # Wait between checks (doesn't block requests).
            await asyncio.sleep(RULES_REFRESH_S)
            try:
                # boto3 is blocking, so run it in a worker thread.
                if await asyncio.to_thread(self._fetch_s3):
                    log.info("rules changed: %d rules", len(self.rules))
            # Bad file or S3 error: keep serving with the last good rules.
            except Exception:
                log.exception("rules refresh failed; keeping previous rules")


def path_matches(pattern, path):
    # "*" matches every path.
    if pattern == "*":
        return True
    # "/x/*" matches /x and everything under it.
    if pattern.endswith("/*"):
        # The part before "/*".
        base = pattern[:-2]
        return path == base or path.startswith(base + "/")
    # Patterns with ? or [ use wildcard matching; anything else must match exactly.
    return fnmatch.fnmatchcase(path, pattern) if any(c in pattern for c in "?[") else path == pattern


# --- Caller identity --------------------------------------------------------


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


def caller_ids(scope):
    """Return {"user_id": .., "cookie": .., "ip": ..}; missing ones are None.

    Layering: cookie is used only when there is no user id. IP is always set.
    """
    # User id from the X-User-Id header (test only: callers can fake this).
    user = header(scope, "x-user-id")
    # Keep it only if it looks like a safe id.
    user = user if user and CALLER_ID.match(user) else None

    # No cookie id unless we find a valid one below.
    cookie = None
    # Cookie layer only applies when there's no user id.
    if user is None:
        # The raw Cookie header, e.g. "session=abc123; theme=dark".
        raw = header(scope, "cookie")
        if raw:
            try:
                # Pick the "session" cookie out of the header.
                morsel = SimpleCookie(raw).get("session")
            except CookieError:
                # Malformed cookie header: treat as no cookie.
                morsel = None
            # Keep it only if it looks like a safe id.
            if morsel and CALLER_ID.match(morsel.value):
                cookie = morsel.value

    # nginx sets X-Real-IP from the real TCP peer. This container publishes no
    # port, so only nginx can reach it and the header can be trusted here.
    # Fallback: the direct peer address (local testing without nginx).
    ip_text = header(scope, "x-real-ip") or (scope.get("client") or ("unknown", 0))[0]
    try:
        # Parse the address text.
        ip = ipaddress.ip_address(ip_text.strip())
        # IPv4 -> number (203.0.113.7 -> 3405803783); IPv6 -> text with "_" for ":".
        ip_id = str(int(ip)) if ip.version == 4 else ip.compressed.replace(":", "_")
    except ValueError:
        # Not a valid IP: all such requests share one "unknown" key.
        ip_id = "unknown"

    # One dict with every identity we found.
    return {"user_id": user, "cookie": cookie, "ip": ip_id}


def keys_for(rules, path, ids):
    """Matching rules -> (keys, args, names) for sliding_window_log.lua."""
    # Redis keys, Lua args (2 per key), and rule ids (to report who denied).
    keys, args, names = [], [], []
    # Check every rule.
    for r in rules:
        # The caller id this rule needs, e.g. the user id for "user_id" rules.
        caller = ids.get(r["key"])
        # Skip if we don't have that id, or the path doesn't match.
        if caller is None or not path_matches(r["path"], path):
            continue
        # Key "<rule>-u<caller>", e.g. "user-u42".
        keys.append(f'{r["id"]}-u{caller}')
        # Limit and window for this key, in the order the Lua expects.
        args += [r["limit"], r["window"]]
        # Remember the rule id at the same position.
        names.append(r["id"])
    # Everything the Redis call needs.
    return keys, args, names


# --- Backend ----------------------------------------------------------------


class Backend:
    """Invokes the backend Lambda, or echoes locally when BACKEND_FUNCTION is empty."""

    def __init__(self):
        # Lambda client only if a function is configured; signs calls with the instance role.
        self.client = boto3.client("lambda") if BACKEND_FUNCTION else None

    def _invoke(self, payload):
        # Call the Lambda synchronously and wait for its answer.
        resp = self.client.invoke(FunctionName=BACKEND_FUNCTION, Payload=json.dumps(payload).encode())
        # The Lambda's response body.
        body = resp["Payload"].read()
        # FunctionError is set if the Lambda crashed -> 502 Bad Gateway.
        status = 502 if resp.get("FunctionError") else 200
        # Status and body for the client.
        return status, body

    async def call(self, payload):
        # No Lambda configured: echo the request (local testing).
        if self.client is None:
            return 200, json.dumps({"echo": payload}).encode()
        # boto3 is blocking, so run it in a worker thread.
        return await asyncio.to_thread(self._invoke, payload)


# --- App (plain ASGI, no framework, to keep per-request overhead small) ------


class App:
    def __init__(self, redis_client=None):
        # Redis connection pool (connects on first use). Tests can pass a fake client.
        self.redis = redis_client or aioredis.from_url(REDIS_URL)
        # Load the shared Lua algorithm from disk.
        with open(LUA_PATH, encoding="utf-8") as f:
            # register_script sends EVALSHA and reloads the script if Redis lost it.
            self.script = self.redis.register_script(f.read())
        # In-memory rules (loaded in startup()).
        self.rules = Rules()
        # Backend caller (Lambda or echo).
        self.backend = Backend()

    async def startup(self):
        # Load rules before taking traffic (boto3/file IO in a worker thread).
        await asyncio.to_thread(self.rules.load_initial)
        # With S3, keep checking for rule changes in the background.
        if self.rules.s3:
            self._refresh_task = asyncio.create_task(self.rules.refresh_forever())

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
        await self.handle(scope, send)

    async def handle(self, scope, send):
        # Start the clock for this request.
        t0 = time.perf_counter()
        # Full path from nginx, e.g. "/python/orders".
        path = scope["path"]
        # Strip the language prefix: "/python/orders" -> "/orders".
        if path.startswith(PATH_PREFIX):
            path = path[len(PATH_PREFIX):] or "/"

        # Health check: no rate limiting, no backend.
        if path == "/healthz":
            return await respond(send, 200, b'{"ok":true}', [])

        # 1. Rules + identity (memory only, no network).
        ids = caller_ids(scope)
        keys, args, names = keys_for(self.rules.rules, path, ids)
        # Time after step 1.
        t1 = time.perf_counter()

        # 2. One Redis round trip for every key.
        # Defaults if no rule matched: allowed, unknown remaining, no wait.
        allowed, remaining, retry_ms, denied_by = 1, -1, 0, 0
        if keys:
            # Runs sliding_window_log.lua on all keys (request id first); returns 4 integers.
            allowed, remaining, retry_ms, denied_by = await self.script(keys=keys, args=[uuid.uuid4().hex] + args)
        # Time after step 2.
        t2 = time.perf_counter()

        # Durations so far (seconds; converted to ms in the header).
        timing = [("rules", t1 - t0), ("redis", t2 - t1)]
        # Tell the client how many requests are left in its fullest window.
        headers = [(b"x-ratelimit-remaining", str(remaining).encode())]

        # Denied: answer 429 without calling the backend.
        if not allowed:
            # Which rule said no (denied_by is 1-based; 0 = none).
            rule = names[denied_by - 1] if denied_by else ""
            headers += [(b"x-ratelimit-rule", rule.encode())]
            # Retry-After in whole seconds, rounded up, at least 1.
            headers += [(b"retry-after", str(max(1, -(-retry_ms // 1000))).encode())]
            # Total time for the denied request.
            timing.append(("total", time.perf_counter() - t0))
            # JSON body explaining the denial.
            body = json.dumps({"error": "rate limited", "rule": rule, "retry_after_ms": retry_ms}).encode()
            # Send the 429.
            return await respond(send, 429, body, headers + [server_timing(timing)])

        # 3. Allowed: call the backend with the path and method.
        status, body = await self.backend.call({"path": path, "method": scope["method"]})
        # Time after step 3.
        t3 = time.perf_counter()
        # Add backend and total durations.
        timing += [("backend", t3 - t2), ("total", t3 - t0)]
        # Send the backend's answer to the client.
        return await respond(send, status, body, headers + [server_timing(timing)])


def server_timing(parts):
    """Standard Server-Timing header, durations in ms."""
    # e.g. "rules;dur=0.028, redis;dur=0.410, total;dur=0.450"
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
