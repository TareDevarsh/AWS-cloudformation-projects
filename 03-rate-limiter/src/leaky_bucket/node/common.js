// Pieces shared by the Node leaky bucket limiter (server.js) and worker (worker.js).
//
// Only plain helpers live here: config from environment variables, reading
// rules.yaml (from S3 or a local file), the Redis scripts and a few name
// builders. Everything that talks to a user (HTTP) is in server.js; everything
// that paces jobs is in worker.js. Same job as python/common.py.
//
// Config (environment variables):
//   REDIS_URL          redis://redis:6379/0
//   LANGUAGE           node (part of every queue and Redis key name)
//   ACCEPT_LUA         path to accept.lua
//   FINISH_LUA         path to finish.lua
//   TOKEN_BUCKET_LUA   path to the EXISTING token bucket script (polling limit)
//   RULES_FILE         local rules.yaml (used if RULES_BUCKET is empty)
//   RULES_BUCKET       S3 bucket holding the rules (optional)
//   RULES_KEY          S3 object key (default rules.yaml)
//   RULES_REFRESH_S    seconds between S3 ETag checks (default 30)
//   SQS_PREFIX         queue URL up to the language, e.g.
//                      https://sqs.<region>.amazonaws.com/<account>/rate-limiter-dev-
//                      (queue url = SQS_PREFIX + "<language>-t<tier>.fifo")

"use strict"; // stricter JavaScript: catches common mistakes

// --- Imports -----------------------------------------------------------------

const fs = require("node:fs"); // reads the Lua and rules files
const path = require("node:path"); // builds file paths
const yaml = require("js-yaml"); // parses rules.yaml
const { S3Client, GetObjectCommand } = require("@aws-sdk/client-s3"); // reads rules from S3

// --- Config (from environment variables, with local defaults) ------------------

// Shortcut to environment variables.
const env = process.env;
// Where Redis is. In Docker Compose: redis://:<password>@redis:6379/0.
const REDIS_URL = env.REDIS_URL || "redis://127.0.0.1:6379/0";
// Which language this process is; every queue and Redis key name contains it.
const LANGUAGE = env.LANGUAGE || "node";
// The two Lua scripts of this algorithm (by default the files one folder up).
const ACCEPT_LUA = env.ACCEPT_LUA || path.join(__dirname, "..", "accept.lua");
const FINISH_LUA = env.FINISH_LUA || path.join(__dirname, "..", "finish.lua");
// The EXISTING token bucket script, reused as-is for the polling limit.
const TOKEN_BUCKET_LUA = env.TOKEN_BUCKET_LUA || path.join(__dirname, "..", "..", "token_bucket", "token_bucket.lua");
// Local rules file, used when no S3 bucket is set (local testing).
const RULES_FILE = env.RULES_FILE || path.join(__dirname, "..", "rules.yaml");
// S3 bucket with the rules. Empty = use the local file.
const RULES_BUCKET = env.RULES_BUCKET || "";
// Name of the rules object inside the bucket.
const RULES_KEY = env.RULES_KEY || "rules.yaml";
// How often to ask S3 "did the rules change?".
const RULES_REFRESH_S = Number(env.RULES_REFRESH_S || "30");
// Queue URL up to (and including) the "-" before the language.
const SQS_PREFIX = env.SQS_PREFIX || "";

// Caller ids from headers: safe characters, max 128. Junk is rejected.
const CALLER_ID = /^[A-Za-z0-9_.:]{1,128}$/;
// Job ids are UUIDs made by the limiter: lowercase hex in 8-4-4-4-12 groups.
const JOB_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// Seconds a job record lives in Redis (5 minutes), counted again from the end of the job.
const JOB_TTL_S = 300;
// Extra seconds a user's line count lives beyond the SQS retention (retries take a few seconds).
const LINE_TTL_CUSHION_S = 60;

// --- Rules ---------------------------------------------------------------------

// Validate rules.yaml text and return a plain object of settings.
function parseRules(text) {
  // JSON_SCHEMA: plain data only, no custom YAML types; empty file -> {}.
  const data = yaml.load(text, { schema: yaml.JSON_SCHEMA }) || {};

  // --- queue settings ---
  const q = data.queue || {};
  // Whole numbers of seconds / attempts, with the approved defaults.
  const queue = {
    retention_s: Math.trunc(Number(q.retention_s ?? 120)),
    max_attempts: Math.trunc(Number(q.max_attempts ?? 5)),
    visibility_timeout_s: Math.trunc(Number(q.visibility_timeout_s ?? 10)),
    retry_visibility_s: Math.trunc(Number(q.retry_visibility_s ?? 1)),
  };
  // All of them must be at least 1 (NaN fails ">=").
  if (!Object.values(queue).every((v) => v >= 1)) throw new Error("queue settings must all be >= 1");

  // --- poll limits (token bucket sizes and rates) ---
  const p = data.poll || {};
  // Defaults: per job 3 / 1 per s, per user 20 / 10 per s. Each is [bucket_size, refill_rate].
  const poll = {
    job: [Number((p.job || {}).bucket_size ?? 3), Number((p.job || {}).refill_rate ?? 1)],
    user: [Number((p.user || {}).bucket_size ?? 20), Number((p.user || {}).refill_rate ?? 10)],
  };
  // Sizes and rates must be positive (the token bucket script checks too).
  if (!Object.values(poll).every((pair) => pair.every((v) => v > 0))) {
    throw new Error("poll bucket_size and refill_rate must be > 0");
  }

  // --- tiers ---
  // Map keeps the order of the file: tier number -> settings.
  const tiers = new Map();
  // One entry per tier in the file.
  for (const t of data.tiers || []) {
    // Tier number, 0-255.
    const id = Math.trunc(Number(t.id));
    if (!(id >= 0 && id <= 255)) throw new Error(`tier id must be 0-255: ${t.id}`);
    // Leak rate (jobs/s) and per-user line length.
    const rate = Number(t.leak_rate);
    const maxWaiting = Math.trunc(Number(t.max_waiting));
    if (!(rate > 0 && maxWaiting >= 1)) throw new Error(`tier ${id}: leak_rate must be > 0 and max_waiting >= 1`);
    // Reject a second entry for the same tier.
    if (tiers.has(id)) throw new Error(`tier ${id} is listed twice`);
    // Flat, ready-to-use version of the tier.
    tiers.set(id, { id, name: String(t.name ?? id), leak_rate: rate, max_waiting: maxWaiting, interval_ms: 1000 / rate });
  }
  // Tier 0 is the fallback for unknown users.
  if (!tiers.has(0)) throw new Error("tier 0 must be configured");

  // --- test-only seed users ---
  // user id -> tier number.
  const seed = {};
  for (const [user, tidRaw] of Object.entries(data.seed_tiers || {})) {
    // Same safe-id rule as the X-User-Id header.
    if (!CALLER_ID.test(String(user))) throw new Error(`seed_tiers: bad user id ${JSON.stringify(user)}`);
    // Only tiers that exist.
    const tid = Math.trunc(Number(tidRaw));
    if (!tiers.has(tid)) throw new Error(`seed_tiers: ${user} uses unknown tier ${tidRaw}`);
    seed[String(user)] = tid;
  }

  // Everything the limiter and worker need.
  return { queue, poll, tiers, seed, agent_sleep_ms: Math.trunc(Number(data.agent_sleep_ms ?? 200)) };
}

// Holds the current rules in memory and refreshes them from S3 by ETag.
class Rules {
  constructor() {
    // Current rules (object from parseRules); replaced as a whole on refresh.
    this.cfg = null;
    // S3 version tag of the rules we hold; null = nothing loaded yet.
    this.etag = null;
    // S3 client only when a bucket is configured (credentials: instance role).
    this.s3 = RULES_BUCKET ? new S3Client({}) : null;
  }

  // Read rules once at startup.
  async loadInitial() {
    if (this.s3) {
      // From S3 (deployed).
      await this.fetchS3();
    } else {
      // From the local file (testing).
      this.cfg = parseRules(fs.readFileSync(RULES_FILE, "utf8"));
    }
    // Confirm in the log how many tiers we have.
    console.log(`loaded rules: ${this.cfg.tiers.size} tiers`);
  }

  // GET the rules only if changed. S3 answers 304 when the ETag matches.
  async fetchS3() {
    // Which object to read; send our version if we have one.
    const input = { Bucket: RULES_BUCKET, Key: RULES_KEY };
    if (this.etag) input.IfNoneMatch = this.etag;
    let obj;
    try {
      // Read the object (or get a 304 "not modified").
      obj = await this.s3.send(new GetObjectCommand(input));
    } catch (err) {
      // 304 = unchanged: keep the rules we have.
      if (err.$metadata && err.$metadata.httpStatusCode === 304) return false;
      // Anything else goes to the caller.
      throw err;
    }
    // Changed: parse, then swap in the new rules in one assignment.
    this.cfg = parseRules(await obj.Body.transformToString());
    // Remember this version for the next check.
    this.etag = obj.ETag;
    // Tell the caller the rules changed.
    return true;
  }

  // Background timer: check S3 every RULES_REFRESH_S. Keeps old rules on error.
  startRefresh(onChange) {
    setInterval(async () => {
      try {
        if (await this.fetchS3()) {
          console.log(`rules changed: ${this.cfg.tiers.size} tiers`);
          // Let the caller react (the limiter re-applies the seed users).
          if (onChange) await onChange();
        }
      } catch (err) {
        // Bad file or S3 error: keep serving with the last good rules.
        console.error("rules refresh failed; keeping previous rules:", err.message);
      }
    }, RULES_REFRESH_S * 1000).unref(); // unref: the timer alone doesn't keep the process alive
  }
}

// --- Redis scripts -------------------------------------------------------------

// Attach Lua scripts to a Redis client as methods (ioredis sends EVALSHA and
// falls back to EVAL if Redis lost the script). `which` lists the ones needed:
//   accept(tiersKey, ...args)                   accept.lua
//   finish(jobKey, status, text, jobTtl)        finish.lua
//   pollLimit(jobBucket, userBucket, ...args)   token_bucket.lua
function defineScripts(redis, which) {
  // Door guard: 1 key (the tiers hash).
  if (which.includes("accept")) redis.defineCommand("accept", { numberOfKeys: 1, lua: fs.readFileSync(ACCEPT_LUA, "utf8") });
  // Job bookkeeping: 1 key (the job record).
  if (which.includes("finish")) redis.defineCommand("finish", { numberOfKeys: 1, lua: fs.readFileSync(FINISH_LUA, "utf8") });
  // Polling limit: the existing token bucket script on exactly 2 keys (job bucket, user bucket).
  if (which.includes("pollLimit")) redis.defineCommand("pollLimit", { numberOfKeys: 2, lua: fs.readFileSync(TOKEN_BUCKET_LUA, "utf8") });
  return redis;
}

// --- Small helpers -------------------------------------------------------------

// Redis key of a job record, e.g. job-6f1c.... Same name in accept.lua ("job-" .. id).
function jobKey(jobId) {
  return `job-${jobId}`;
}

// SQS FIFO queue of this language and tier, e.g. <prefix>node-t1.fifo.
// One queue per language and tier; the prefix carries region, account and stage.
function queueUrl(tier) {
  return `${SQS_PREFIX}${LANGUAGE}-t${tier}.fifo`;
}

// Exported for server.js, worker.js and tests.
module.exports = {
  REDIS_URL, LANGUAGE, JOB_TTL_S, LINE_TTL_CUSHION_S, CALLER_ID, JOB_ID,
  parseRules, Rules, defineScripts, jobKey, queueUrl,
};
