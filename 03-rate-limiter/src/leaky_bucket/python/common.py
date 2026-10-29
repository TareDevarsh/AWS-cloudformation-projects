"""Pieces shared by the Python leaky bucket limiter (app.py) and worker (worker.py).

Only plain helpers live here: config from environment variables, reading
rules.yaml (from S3 or a local file), and a few name builders. Everything that
talks to a user (HTTP) is in app.py; everything that paces jobs is in worker.py.

Config (environment variables):
  REDIS_URL          redis://redis:6379/0
  LANGUAGE           python (part of every queue and Redis key name)
  ACCEPT_LUA         path to accept.lua
  FINISH_LUA         path to finish.lua
  RULES_FILE         local rules.yaml (used if RULES_BUCKET is empty)
  RULES_BUCKET       S3 bucket holding the rules (optional)
  RULES_KEY          S3 object key (default rules.yaml)
  RULES_REFRESH_S    seconds between S3 ETag checks (default 30)
  SQS_PREFIX         queue URL up to the language, e.g.
                     https://sqs.<region>.amazonaws.com/<account>/rate-limiter-dev-
                     (queue url = SQS_PREFIX + "<language>-t<tier>.fifo")
"""

# --- Imports ----------------------------------------------------------------

import asyncio  # async event loop: the S3 refresh task sleeps between checks
import logging  # startup and error logs (not per request, to keep it fast)
import os  # reads environment variables (config)
import re  # regular expressions to validate ids

import boto3  # AWS SDK: S3 (rules)
import yaml  # reads rules.yaml
from botocore.exceptions import ClientError  # error type boto3 raises (e.g. S3 304)

# Logger named "leaky" so our lines are easy to find (limiter and worker both use it).
log = logging.getLogger("leaky")
# Log format: time, level, message.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --- Config (from environment variables, with local defaults) ---------------

# Folder of this file; the default file paths below are relative to it.
HERE = os.path.dirname(os.path.abspath(__file__))
# Where Redis is. In Docker Compose this is redis://:<password>@redis:6379/0.
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
# Which language this process is; every queue and Redis key name contains it.
LANGUAGE = os.environ.get("LANGUAGE", "python")
# The two Lua scripts of this algorithm (by default the files one folder up).
ACCEPT_LUA = os.environ.get("ACCEPT_LUA", os.path.join(HERE, "..", "accept.lua"))
FINISH_LUA = os.environ.get("FINISH_LUA", os.path.join(HERE, "..", "finish.lua"))
# Local rules file, used when no S3 bucket is set (local testing).
RULES_FILE = os.environ.get("RULES_FILE", os.path.join(HERE, "..", "rules.yaml"))
# S3 bucket with the rules. Empty = use the local file.
RULES_BUCKET = os.environ.get("RULES_BUCKET", "")
# Name of the rules object inside the bucket.
RULES_KEY = os.environ.get("RULES_KEY", "rules.yaml")
# How often to ask S3 "did the rules change?".
RULES_REFRESH_S = float(os.environ.get("RULES_REFRESH_S", "30"))
# Queue URL up to (and including) the "-" before the language.
SQS_PREFIX = os.environ.get("SQS_PREFIX", "")

# Caller ids from headers: safe characters, max 128. Junk is rejected.
CALLER_ID = re.compile(r"^[A-Za-z0-9_.:]{1,128}$")
# Job ids are UUIDs made by the limiter: lowercase hex in 8-4-4-4-12 groups.
JOB_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Seconds a job record lives in Redis (5 minutes), counted again from the end of the job.
JOB_TTL_S = 300
# Extra seconds a user's line count lives beyond the SQS retention (retries take a few seconds).
LINE_TTL_CUSHION_S = 60

# --- Rules ------------------------------------------------------------------


def parse_rules(text):
    """Validate rules.yaml text and return a plain dict of settings."""
    # safe_load never runs code from the file; empty file -> {}.
    data = yaml.safe_load(text) or {}

    # --- queue settings ---
    q = data.get("queue", {})
    # Whole numbers of seconds / attempts, with the approved defaults.
    queue = {"retention_s": int(q.get("retention_s", 120)),
             "max_attempts": int(q.get("max_attempts", 5)),
             "visibility_timeout_s": int(q.get("visibility_timeout_s", 10)),
             "retry_visibility_s": int(q.get("retry_visibility_s", 1))}
    # All of them must be at least 1.
    if min(queue.values()) < 1:
        raise ValueError("queue settings must all be >= 1")

    # --- poll limits (token bucket sizes and rates) ---
    p = data.get("poll", {})
    # Defaults: per job 3 / 1 per s, per user 20 / 10 per s.
    poll = {"job": (float(p.get("job", {}).get("bucket_size", 3)), float(p.get("job", {}).get("refill_rate", 1))),
            "user": (float(p.get("user", {}).get("bucket_size", 20)), float(p.get("user", {}).get("refill_rate", 10)))}
    # Sizes and rates must be positive (the token bucket script checks too).
    if min(min(v) for v in poll.values()) <= 0:
        raise ValueError("poll bucket_size and refill_rate must be > 0")

    # --- tiers ---
    tiers = {}
    # One entry per tier in the file.
    for t in data.get("tiers", []):
        # Tier number, 0-255.
        tid = int(t["id"])
        if not 0 <= tid <= 255:
            raise ValueError(f"tier id must be 0-255: {tid}")
        # Leak rate (jobs/s) and per-user line length.
        rate, max_waiting = float(t["leak_rate"]), int(t["max_waiting"])
        if rate <= 0 or max_waiting < 1:
            raise ValueError(f"tier {tid}: leak_rate must be > 0 and max_waiting >= 1")
        # Reject a second entry for the same tier.
        if tid in tiers:
            raise ValueError(f"tier {tid} is listed twice")
        # Flat, ready-to-use version of the tier.
        tiers[tid] = {"id": tid, "name": str(t.get("name", tid)), "leak_rate": rate,
                      "max_waiting": max_waiting, "interval_ms": 1000.0 / rate}
    # Tier 0 is the fallback for unknown users.
    if 0 not in tiers:
        raise ValueError("tier 0 must be configured")

    # --- test-only seed users ---
    seed = {}
    # user id -> tier number.
    for user, tid in (data.get("seed_tiers") or {}).items():
        # Same safe-id rule as the X-User-Id header.
        if not CALLER_ID.match(str(user)):
            raise ValueError(f"seed_tiers: bad user id {user!r}")
        # Only tiers that exist.
        if int(tid) not in tiers:
            raise ValueError(f"seed_tiers: {user} uses unknown tier {tid}")
        seed[str(user)] = int(tid)

    # Everything the limiter and worker need.
    return {"queue": queue, "poll": poll, "tiers": tiers, "seed": seed,
            "agent_sleep_ms": int(data.get("agent_sleep_ms", 200))}


class Rules:
    """Holds the current rules in memory and refreshes them from S3 by ETag."""

    def __init__(self):
        # Current rules (dict from parse_rules); replaced as a whole on refresh.
        self.cfg = None
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
                self.cfg = parse_rules(f.read())
        # Confirm in the log how many tiers we have.
        log.info("loaded rules: %d tiers", len(self.cfg["tiers"]))

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
        self.cfg = parse_rules(obj["Body"].read().decode("utf-8"))
        # Remember this version for the next check.
        self.etag = obj["ETag"]
        # Tell the caller the rules changed.
        return True

    async def refresh_forever(self, on_change=None):
        """Background task: check S3 every RULES_REFRESH_S. Keeps old rules on error."""
        # Runs for the life of the server.
        while True:
            # Wait between checks (doesn't block requests).
            await asyncio.sleep(RULES_REFRESH_S)
            try:
                # boto3 is blocking, so run it in a worker thread.
                if await asyncio.to_thread(self._fetch_s3):
                    log.info("rules changed: %d tiers", len(self.cfg["tiers"]))
                    # Let the caller react (the limiter re-applies the seed users).
                    if on_change:
                        await on_change()
            # Bad file or S3 error: keep serving with the last good rules.
            except Exception:
                log.exception("rules refresh failed; keeping previous rules")


# --- Small helpers ----------------------------------------------------------


def read_text(path):
    """Whole text of a small file (the Lua scripts)."""
    # utf-8 so the comments in the scripts read the same everywhere.
    with open(path, encoding="utf-8") as f:
        return f.read()


def job_key(job_id):
    """Redis key of a job record, e.g. job-6f1c...."""
    # Same name in accept.lua ("job-" .. id) and in the other languages.
    return f"job-{job_id}"


def queue_url(tier):
    """SQS FIFO queue of this language and tier, e.g. <prefix>python-t1.fifo."""
    # One queue per language and tier; the prefix carries region, account and stage.
    return f"{SQS_PREFIX}{LANGUAGE}-t{tier}.fifo"
