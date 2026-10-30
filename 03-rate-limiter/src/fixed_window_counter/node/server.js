// Fixed window counter rate limiter server, Node version.
//
// Same behaviour as python/app.py:
//   1. nginx sends /node/<path> here.
//   2. Work out who the caller is: user id, session cookie, IP.
//   3. Pick the matching rules (kept in memory) and build one Redis key each.
//   4. One Redis call runs fixed_window_counter.lua on all keys (all-or-nothing).
//   5. Allowed -> invoke the backend Lambda. Denied -> 429.
//
// Every response carries a Server-Timing header with per-step durations (ms).
//
// Config (environment variables): REDIS_URL, LUA_PATH, RULES_FILE,
// RULES_BUCKET, RULES_KEY, RULES_REFRESH_S, BACKEND_FUNCTION,
// PATH_PREFIX (default /node), HOST, PORT (default 127.0.0.1:8000).

"use strict"; // stricter JavaScript: catches common mistakes

// --- Imports -----------------------------------------------------------------

const fs = require("node:fs"); // reads the Lua and rules files
const http = require("node:http"); // the HTTP server (built in, no framework)
const net = require("node:net"); // checks IPv4 vs IPv6
const path = require("node:path"); // builds file paths
const Redis = require("ioredis"); // Redis client
const yaml = require("js-yaml"); // parses rules.yaml
const { S3Client, GetObjectCommand } = require("@aws-sdk/client-s3"); // reads rules from S3
const { LambdaClient, InvokeCommand } = require("@aws-sdk/client-lambda"); // invokes the backend

// --- Config ------------------------------------------------------------------

// Shortcut to environment variables.
const env = process.env;
// Where Redis is. In Docker Compose: redis://redis:6379/0.
const REDIS_URL = env.REDIS_URL || "redis://127.0.0.1:6379/0";
// The shared Lua algorithm.
const LUA_PATH = env.LUA_PATH || path.join(__dirname, "..", "fixed_window_counter.lua");
// Local rules file, used when no S3 bucket is set.
const RULES_FILE = env.RULES_FILE || path.join(__dirname, "..", "rules.yaml");
// S3 bucket with the rules. Empty = use the local file.
const RULES_BUCKET = env.RULES_BUCKET || "";
// Name of the rules object in the bucket.
const RULES_KEY = env.RULES_KEY || "rules.yaml";
// Seconds between "did the rules change?" checks.
const RULES_REFRESH_S = Number(env.RULES_REFRESH_S || "30");
// Backend Lambda name. Empty = echo the request back.
const BACKEND_FUNCTION = env.BACKEND_FUNCTION || "";
// URL prefix nginx uses for this language; stripped before matching.
const PATH_PREFIX = (env.PATH_PREFIX || "/node").replace(/\/+$/, "");

// Rule ids: lowercase letters and digits only, so "<id>-u<caller>" has one meaning.
const RULE_ID = /^[a-z0-9]+$/;
// Caller ids from headers/cookies: safe characters, max 128. Junk is ignored.
const CALLER_ID = /^[A-Za-z0-9_.:]{1,128}$/;
// The identity types a rule can use.
const VALID_KEYS = new Set(["user_id", "cookie", "ip"]);

// --- Rules (kept in memory) --------------------------------------------------

// Validate rules.yaml text and return a list of rules.
function parseRules(text) {
  // JSON_SCHEMA: plain data only, no custom YAML types.
  const data = yaml.load(text, { schema: yaml.JSON_SCHEMA }) || {};
  // Map each raw rule to a validated one.
  return (data.rules || []).map((r) => {
    // Rule id as text, e.g. "user".
    const id = String(r.id);
    // Reject ids that would make Redis keys ambiguous.
    if (!RULE_ID.test(id)) throw new Error(`rule id must be [a-z0-9]+: ${id}`);
    // Which identity this rule limits.
    const key = r.match && r.match.key;
    // Reject unknown identity types.
    if (!VALID_KEYS.has(key)) throw new Error(`rule ${id}: match.key must be user_id, cookie or ip`);
    // Requests per window and window length as numbers.
    const limit = Number(r.limit);
    const window = Number(r.window_s);
    // Limit: whole number >= 1; window: > 0 (NaN fails both checks).
    if (!(Number.isInteger(limit) && limit >= 1 && window > 0)) {
      throw new Error(`rule ${id}: limit must be a whole number > 0 and window_s > 0`);
    }
    // Flat, ready-to-use rule; missing path = every path.
    return { id, key, path: String(r.match.path || "*"), limit, window };
  });
}

// Does a request path match a rule pattern?
function pathMatches(pattern, p) {
  // "*" matches every path.
  if (pattern === "*") return true;
  // "/x/*" matches /x and everything under it.
  if (pattern.endsWith("/*")) {
    const base = pattern.slice(0, -2);
    return p === base || p.startsWith(base + "/");
  }
  // Anything else must match exactly. (Python/Go also support ? and [ ]
  // wildcards; none of our rules use them.)
  return p === pattern;
}

// Holds the current rules and refreshes them from S3 by ETag.
class Rules {
  constructor() {
    // Current rules; replaced as a whole on refresh.
    this.rules = [];
    // S3 version of the rules we hold.
    this.etag = null;
    // S3 client only when a bucket is configured (credentials: instance role).
    this.s3 = RULES_BUCKET ? new S3Client({}) : null;
  }

  // Read rules once at startup.
  async loadInitial() {
    if (this.s3) {
      // From S3.
      await this.fetchS3();
    } else {
      // From the local file.
      this.rules = parseRules(fs.readFileSync(RULES_FILE, "utf8"));
    }
    console.log(`loaded ${this.rules.length} rules`);
  }

  // Read the rules only if they changed (S3 answers 304 when the ETag matches).
  async fetchS3() {
    // Which object to read; send our version if we have one.
    const input = { Bucket: RULES_BUCKET, Key: RULES_KEY };
    if (this.etag) input.IfNoneMatch = this.etag;
    let obj;
    try {
      // Read the object.
      obj = await this.s3.send(new GetObjectCommand(input));
    } catch (err) {
      // 304 = unchanged: keep the rules we have.
      if (err.$metadata && err.$metadata.httpStatusCode === 304) return false;
      // Anything else goes to the caller.
      throw err;
    }
    // Changed: parse, then swap in the new rules in one assignment.
    this.rules = parseRules(await obj.Body.transformToString());
    // Remember this version for the next check.
    this.etag = obj.ETag;
    return true;
  }

  // Background timer: check S3 every N seconds. Keeps old rules on error.
  startRefresh() {
    setInterval(async () => {
      try {
        if (await this.fetchS3()) console.log(`rules changed: ${this.rules.length} rules`);
      } catch (err) {
        console.error("rules refresh failed; keeping previous rules:", err.message);
      }
    }, RULES_REFRESH_S * 1000).unref(); // unref: the timer alone doesn't keep the process alive
  }
}

// --- Caller identity ---------------------------------------------------------

// Read one cookie value from the Cookie header.
function getCookie(header, name) {
  // No Cookie header.
  if (!header) return null;
  // Cookies look like "a=1; session=abc; b=2".
  for (const part of header.split(";")) {
    // Split at the first "=".
    const i = part.indexOf("=");
    if (i < 0) continue;
    // Found our cookie: return its value without spaces.
    if (part.slice(0, i).trim() === name) return part.slice(i + 1).trim();
  }
  // Not present.
  return null;
}

// Work out who is calling: { user_id, cookie, ip }; missing ones are null.
function callerIds(req) {
  // User id from the X-User-Id header (test only: callers can fake this).
  let user = req.headers["x-user-id"];
  // Keep it only if it looks like a safe id.
  user = typeof user === "string" && CALLER_ID.test(user) ? user : null;

  // Cookie layer only applies when there's no user id.
  let cookie = null;
  if (user === null) {
    // The "session" cookie, kept only if it looks safe.
    const c = getCookie(req.headers.cookie, "session");
    if (c && CALLER_ID.test(c)) cookie = c;
  }

  // nginx sets X-Real-IP from the real TCP peer. This container publishes no
  // port, so only nginx can reach it and the header can be trusted here.
  // Fallback: the direct peer address (local testing without nginx).
  const ipText = String(req.headers["x-real-ip"] || req.socket.remoteAddress || "").trim()
    .replace(/^::ffff:/, ""); // Node shows IPv4 peers as "::ffff:192.0.2.4"
  let ip;
  if (net.isIPv4(ipText)) {
    // IPv4 -> number (203.0.113.7 -> 3405803783). ">>> 0" keeps it unsigned.
    ip = String(ipText.split(".").reduce((n, octet) => ((n << 8) | Number(octet)) >>> 0, 0));
  } else if (net.isIPv6(ipText)) {
    // IPv6 -> text with "_" instead of ":".
    ip = ipText.toLowerCase().replace(/:/g, "_");
  } else {
    // Not a valid IP: all such requests share one "unknown" key.
    ip = "unknown";
  }

  // Every identity we found.
  return { user_id: user, cookie, ip };
}

// Matching rules -> Redis keys, Lua args (2 per key) and rule ids.
function keysFor(rules, p, ids) {
  const keys = [];
  const args = [];
  const names = [];
  // Check every rule.
  for (const r of rules) {
    // The caller id this rule needs.
    const caller = ids[r.key];
    // Skip if we don't have that id, or the path doesn't match.
    if (!caller || !pathMatches(r.path, p)) continue;
    // Key "<rule>-u<caller>", e.g. "user-u42".
    keys.push(`${r.id}-u${caller}`);
    // Limit and window for this key, in the order the Lua expects.
    args.push(r.limit, r.window);
    // Remember the rule id at the same position.
    names.push(r.id);
  }
  return { keys, args, names };
}

// --- Backend -----------------------------------------------------------------

// Lambda client only when a function is configured (credentials: instance role).
const lambda = BACKEND_FUNCTION ? new LambdaClient({}) : null;

// Invoke the backend Lambda, or echo locally.
async function callBackend(payload) {
  // No Lambda: echo (local testing).
  if (!lambda) return { status: 200, body: JSON.stringify({ echo: payload }) };
  try {
    // Call the Lambda and wait for its answer.
    const out = await lambda.send(new InvokeCommand({
      FunctionName: BACKEND_FUNCTION,
      Payload: Buffer.from(JSON.stringify(payload)),
    }));
    // The Lambda's response body.
    const body = Buffer.from(out.Payload || []).toString();
    // FunctionError is set if the Lambda crashed -> 502.
    return { status: out.FunctionError ? 502 : 200, body };
  } catch (err) {
    // Couldn't reach the backend.
    console.error("backend error:", err.message);
    return { status: 502, body: '{"error":"backend unavailable"}' };
  }
}

// --- Server ------------------------------------------------------------------

// Build the Server-Timing header from [name, ms] pairs.
function serverTiming(parts) {
  return parts.map(([n, ms]) => `${n};dur=${ms.toFixed(3)}`).join(", ");
}

// Milliseconds since a start time from process.hrtime.bigint().
function msSince(start) {
  return Number(process.hrtime.bigint() - start) / 1e6;
}

// Create the request handler around a Redis client and rules.
function makeHandler(redis, rules) {
  return async function handle(req, res) {
    // Start the clock (nanosecond resolution).
    const t0 = process.hrtime.bigint();
    // Path without the query string, e.g. "/node/orders".
    let p = req.url.split("?")[0];
    // Strip the language prefix: "/node/orders" -> "/orders".
    if (p.startsWith(PATH_PREFIX)) p = p.slice(PATH_PREFIX.length) || "/";
    // All responses are JSON.
    res.setHeader("Content-Type", "application/json");

    // Health check: no rate limiting, no backend.
    if (p === "/healthz") return res.end('{"ok":true}');

    // 1. Rules + identity (memory only).
    const { keys, args, names } = keysFor(rules.rules, p, callerIds(req));
    const t1 = process.hrtime.bigint();

    // 2. One Redis round trip for every key.
    // Defaults if no rule matched: allowed, unknown remaining, no wait.
    let [allowed, remaining, retryMs, deniedBy] = [1, -1, 0, 0];
    if (keys.length) {
      try {
        // Runs fixed_window_counter.lua on all keys (EVALSHA; reloads if Redis lost it).
        [allowed, remaining, retryMs, deniedBy] = await redis.fixedWindow(keys.length, ...keys, ...args);
      } catch (err) {
        // Redis down or script error: fail with 503 (fail-open/closed is parked).
        console.error("redis error:", err.message);
        res.statusCode = 503;
        return res.end('{"error":"rate limiter unavailable"}');
      }
    }
    const t2 = process.hrtime.bigint();

    // Durations so far.
    const timing = [["rules", Number(t1 - t0) / 1e6], ["redis", Number(t2 - t1) / 1e6]];
    // Requests left in the fullest window.
    res.setHeader("X-RateLimit-Remaining", String(remaining));

    // Denied: answer 429 without calling the backend.
    if (!allowed) {
      // Which rule said no (deniedBy is 1-based; 0 = none).
      const rule = deniedBy ? names[deniedBy - 1] : "";
      res.setHeader("X-RateLimit-Rule", rule);
      // Retry-After in whole seconds, rounded up, at least 1.
      res.setHeader("Retry-After", String(Math.max(1, Math.ceil(retryMs / 1000))));
      // Total time and the header.
      timing.push(["total", msSince(t0)]);
      res.setHeader("Server-Timing", serverTiming(timing));
      // Send the 429 with a JSON explanation.
      res.statusCode = 429;
      return res.end(JSON.stringify({ error: "rate limited", rule, retry_after_ms: retryMs }));
    }

    // 3. Allowed: call the backend.
    const { status, body } = await callBackend({ path: p, method: req.method });
    timing.push(["backend", msSince(t2)], ["total", msSince(t0)]);
    res.setHeader("Server-Timing", serverTiming(timing));
    // Send the backend's answer.
    res.statusCode = status;
    res.end(body);
  };
}

// Attach fixed_window_counter.lua to a Redis client as redis.fixedWindow(numKeys, ...keys, ...args).
function defineScript(redis) {
  // No numberOfKeys here: we pass it on each call, since it varies per request.
  redis.defineCommand("fixedWindow", { lua: fs.readFileSync(LUA_PATH, "utf8") });
  return redis;
}

// Start everything.
async function main() {
  // Redis connection (reused by all requests) with the Lua script attached.
  const redis = defineScript(new Redis(REDIS_URL));
  // Load rules before taking traffic.
  const rules = new Rules();
  await rules.loadInitial();
  // With S3, keep checking for changes.
  if (rules.s3) rules.startRefresh();
  // HTTP server using our handler.
  const server = http.createServer(makeHandler(redis, rules));
  // Default is loopback. The Dockerfile sets HOST=0.0.0.0 because inside the
  // container only the Docker network can reach it (no port is published).
  const host = env.HOST || "127.0.0.1";
  const port = Number(env.PORT || "8000");
  server.listen(port, host, () => console.log(`listening on ${host}:${port}`));
}

// Run only when started directly ("node server.js"), not when imported by tests.
if (require.main === module) {
  main().catch((err) => {
    // Startup failed (bad rules, missing file): log and exit non-zero.
    console.error(err);
    process.exit(1);
  });
}

// Exported for tests.
module.exports = { parseRules, pathMatches, callerIds, keysFor, makeHandler, defineScript };
