// Leaky bucket (queue) limiter server, Node version.
//
// This server is the "door": it decides who may join the line and answers polls.
// It does NOT control the speed; the worker (worker.js) starts jobs at a fixed
// rate. Example domain: a chat agent where every message waits for its turn.
// Same behaviour as python/app.py.
//
// Routes (nginx sends /node/<path> here; the prefix is stripped):
//   POST /chat              header X-User-Id, optional JSON {"message": "..."}
//                           202 {job_id, position, tier}  or  429 + Retry-After
//   GET  /result/<job_id>   header X-User-Id (must be the job's owner, else 404)
//                           200 {status: queued|processing|done|failed|expired,
//                                position, answer?, queue_wait_ms?, processing_ms?}
//                           429 + Retry-After if the client polls too fast
//   GET  /healthz           200, no checks
//
// Request flow of POST /chat:
//   1. Validate the user id and the body.
//   2. One Redis call runs accept.lua: tier, room in the user's line?, ticket.
//   3. Full -> 429. Room -> send the job to the SQS FIFO queue of the tier -> 202.
//
// Every response (except /healthz) carries a Server-Timing header, so the laptop
// can see where the time went:  Server-Timing: redis;dur=0.42, sqs;dur=14.2, total;dur=15.0
//
// Config: see common.js, plus
//   PATH_PREFIX        prefix nginx routes on (default /node)
//   HOST / PORT        listen address (default 127.0.0.1:8000)

"use strict"; // stricter JavaScript: catches common mistakes

// --- Imports -----------------------------------------------------------------

const crypto = require("node:crypto"); // randomUUID() for job ids
const http = require("node:http"); // the HTTP server (built in, no framework)
const Redis = require("ioredis"); // Redis client
const { SQSClient, SendMessageCommand } = require("@aws-sdk/client-sqs"); // sends jobs to the line
// Shared helpers: config, rules, scripts, names.
const { LANGUAGE, REDIS_URL, JOB_TTL_S, LINE_TTL_CUSHION_S, CALLER_ID, JOB_ID,
  Rules, defineScripts, jobKey, queueUrl } = require("./common");

// --- Config ------------------------------------------------------------------

// Shortcut to environment variables.
const env = process.env;
// URL prefix nginx uses for this language; stripped before routing.
const PATH_PREFIX = (env.PATH_PREFIX || "/node").replace(/\/+$/, "");
// Longest chat message we accept, in characters.
const MAX_MESSAGE_CHARS = 500;
// Largest request body we read, in bytes (a 500 character message fits many times over).
const MAX_BODY_BYTES = 4096;
// Every job of a queue uses the same group id, so SQS keeps strict first-in, first-out order.
const GROUP_ID = "q";

// --- Small helpers -------------------------------------------------------------

// Small JSON error body, e.g. {"error":"not found"}.
function errorBody(text) {
  return JSON.stringify({ error: text });
}

// Build the Server-Timing header from [name, ms] pairs.
function serverTiming(parts) {
  // e.g. "redis;dur=0.410, sqs;dur=14.200, total;dur=15.000"
  return parts.map(([n, ms]) => `${n};dur=${ms.toFixed(3)}`).join(", ");
}

// Send a JSON response with the exact length; `headers` are extra headers.
function respond(res, status, body, headers = {}) {
  // Body as bytes, so Content-Length counts bytes and not characters.
  const buf = Buffer.from(body);
  // Status line and headers.
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": buf.length, ...headers });
  // Body; the response is complete.
  res.end(buf);
}

// Retry-After in whole seconds, rounded up, at least 1.
function retryAfter(retryMs) {
  return String(Math.max(1, Math.ceil(retryMs / 1000)));
}

// Read the request body as bytes, or resolve null if it is larger than MAX_BODY_BYTES.
function readBody(req) {
  return new Promise((resolve, reject) => {
    // The body can arrive in several pieces.
    const chunks = [];
    let size = 0;
    // A piece arrived: count it and refuse a body that is too large.
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        // Too big: stop collecting, answer null (the caller sends 413) and drop the rest.
        chunks.length = 0;
        req.removeAllListeners("data");
        req.resume();
        resolve(null);
        return;
      }
      chunks.push(chunk);
    });
    // Last piece received: all pieces joined.
    req.on("end", () => resolve(Buffer.concat(chunks)));
    // Client went away or the socket broke.
    req.on("error", reject);
  });
}

// --- The request handler -------------------------------------------------------

// Create the handler around its parts, so tests can pass fakes:
//   redis  an ioredis-like client with accept / finish / pollLimit / pipeline
//   sqs    an object with send(command), like an SQSClient
//   rules  a Rules object (its .cfg holds the current settings)
function makeHandler({ redis, sqs, rules }) {
  // Job record, Redis time (ms) and each tier's "now serving", in one round trip.
  async function readJob(jobId) {
    // Pipeline = send all commands together, get all answers together.
    const pipe = redis.pipeline();
    // The job record (empty object if missing).
    pipe.hgetall(jobKey(jobId));
    // Redis server clock: [seconds, microseconds].
    pipe.time();
    // "Now serving" of every configured tier (we don't know the job's tier yet).
    const tierIds = [...rules.cfg.tiers.keys()];
    for (const tid of tierIds) pipe.get(`sv-${LANGUAGE}-${tid}`);
    // Send, then unpack. Each answer is [error, value]; any error is thrown.
    const res = (await pipe.exec()).map(([err, val]) => {
      if (err) throw err;
      return val;
    });
    // Milliseconds since 1970 from the Redis clock.
    const nowMs = Number(res[1][0]) * 1000 + Math.floor(Number(res[1][1]) / 1000);
    // tier id -> last finished ticket (0 if never set).
    const serving = new Map(tierIds.map((tid, i) => [tid, Number(res[2 + i] || 0)]));
    // No fields at all = no such job.
    const job = Object.keys(res[0]).length ? res[0] : null;
    return { job, nowMs, serving };
  }

  // --- POST /chat --------------------------------------------------------------

  async function chat(req, res, user, t0) {
    // 1. Read and check the body (memory only, no network).
    const body = await readBody(req);
    // Too big: stop before parsing. Close the connection, we did not read the rest.
    if (body === null) {
      return respond(res, 413, errorBody("body too large"),
        { "Connection": "close", "Server-Timing": serverTiming([["total", performance.now() - t0]]) });
    }
    // No body is fine: an empty message.
    let message = "";
    let fail = false;
    if (body.length) {
      try {
        // Must be a JSON object, e.g. {"message": "hi"}. fatal: bad UTF-8 is an error.
        const data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
        if (data === null || typeof data !== "object" || Array.isArray(data)) throw new Error("body must be a JSON object");
        // Missing message = empty message.
        message = data.message === undefined ? "" : data.message;
        // Test hook: {"fail": true} makes the backend raise, to test retries.
        fail = data.fail === true;
        // The message must be text and not too long (length counted in characters, not bytes).
        if (typeof message !== "string" || [...message].length > MAX_MESSAGE_CHARS) {
          throw new Error(`message must be text of at most ${MAX_MESSAGE_CHARS} characters`);
        }
      } catch (e) {
        // Not JSON, not an object, or a bad message: 400.
        return respond(res, 400, errorBody(`bad body: ${e.message}`),
          { "Server-Timing": serverTiming([["total", performance.now() - t0]]) });
      }
    }

    // Current settings (a plain object, replaced as a whole when rules change).
    const cfg = rules.cfg;
    // A random, hard to guess id for this job.
    const jobId = crypto.randomUUID();
    // Lua args: language, user, job id, line count lifetime, job lifetime,
    // then 3 numbers per tier (tier, max_waiting, interval_ms).
    const args = [LANGUAGE, user, jobId, cfg.queue.retention_s + LINE_TTL_CUSHION_S, JOB_TTL_S];
    for (const t of cfg.tiers.values()) args.push(t.id, t.max_waiting, Math.round(t.interval_ms));
    // Time after step 1.
    const t1 = performance.now();

    // 2. One Redis round trip: tier, room in line?, ticket, job record.
    let accepted, tier, position, retryMs;
    try {
      [accepted, tier, , position, retryMs] = await redis.accept("tiers", ...args);
    } catch (err) {
      // Redis down or script error: nothing was written, tell the client.
      console.error("redis error:", err.message);
      return respond(res, 503, errorBody("rate limiter unavailable"),
        { "Server-Timing": serverTiming([["total", performance.now() - t0]]) });
    }
    // Time after step 2.
    const t2 = performance.now();

    // Line full: answer 429 without touching SQS.
    if (!accepted) {
      const timing = [["redis", t2 - t1], ["total", t2 - t0]];
      return respond(res, 429, JSON.stringify({ error: "line full", tier, retry_after_ms: retryMs }),
        { "Retry-After": retryAfter(retryMs), "Server-Timing": serverTiming(timing) });
    }

    // 3. Joined the line: put the job in this tier's SQS FIFO queue.
    try {
      await sqs.send(new SendMessageCommand({
        QueueUrl: queueUrl(tier), // this language and tier
        MessageBody: JSON.stringify({ job: jobId, msg: message, fail }), // what the worker needs
        MessageGroupId: GROUP_ID, // one group = strict first-in, first-out
        MessageDeduplicationId: jobId, // a repeat send of the same job is ignored
      }));
    } catch (err) {
      // Could not queue it: give the place in line back, then tell the client.
      console.error(`SQS send failed for job ${jobId}:`, err.message);
      try {
        await redis.finish(jobKey(jobId), "failed", "could not queue the job", JOB_TTL_S);
      } catch (err2) {
        // The line count then expires on its own (its TTL).
        console.error("could not give the place back:", err2.message);
      }
      return respond(res, 502, errorBody("queue unavailable"),
        { "Server-Timing": serverTiming([["redis", t2 - t1], ["total", performance.now() - t0]]) });
    }
    // Time after step 3.
    const t3 = performance.now();

    // Tell the client where it stands; it polls GET /result/<job_id> from here.
    const timing = [["redis", t2 - t1], ["sqs", t3 - t2], ["total", t3 - t0]];
    return respond(res, 202, JSON.stringify({ job_id: jobId, position, tier }), { "Server-Timing": serverTiming(timing) });
  }

  // --- GET /result/<job_id> -----------------------------------------------------

  async function result(res, user, jobId, t0) {
    // Not even shaped like one of our ids: 404, and Redis is not touched.
    if (!JOB_ID.test(jobId)) {
      return respond(res, 404, errorBody("no such job"), { "Server-Timing": serverTiming([["total", performance.now() - t0]]) });
    }

    // Current settings.
    const cfg = rules.cfg;
    // 1. Polling limit: reuse the token bucket script on two buckets (all-or-nothing):
    //    one per job and one per user. The job bucket also covers unknown ids.
    const [jsize, jrate] = cfg.poll.job;
    const [usize, urate] = cfg.poll.user;
    let allowed, retryMs;
    try {
      [allowed, , retryMs] = await redis.pollLimit(`poll-u${jobId}`, `pollu-u${user}`, jsize, jrate, 1, usize, urate, 1);
    } catch (err) {
      // Redis down or script error.
      console.error("redis error:", err.message);
      return respond(res, 503, errorBody("rate limiter unavailable"),
        { "Server-Timing": serverTiming([["total", performance.now() - t0]]) });
    }
    // Time after step 1.
    const t1 = performance.now();
    // Too many polls: 429 with a wait time.
    if (!allowed) {
      const timing = [["redis", t1 - t0], ["total", t1 - t0]];
      return respond(res, 429, JSON.stringify({ error: "polling too fast", retry_after_ms: retryMs }),
        { "Retry-After": retryAfter(retryMs), "Server-Timing": serverTiming(timing) });
    }

    // 2. Read the job, the Redis clock, and every tier's "now serving" in one round trip.
    let { job, nowMs, serving } = await readJob(jobId);
    // Unknown job (never existed or its record expired) or someone else's job: 404.
    if (!job || job.u !== user) {
      const now = performance.now();
      return respond(res, 404, errorBody("no such job"), { "Server-Timing": serverTiming([["redis", now - t1], ["total", now - t0]]) });
    }

    // 3. A job that is still waiting but older than the SQS retention has lost its
    //    message (SQS dropped it): end it as "expired" and give the place back.
    if ((job.st === "queued" || job.st === "processing") && nowMs - Number(job.t0) > cfg.queue.retention_s * 1000) {
      await redis.finish(jobKey(jobId), "expired", "expired in the queue before it was processed", JOB_TTL_S);
      // Read again: if a worker finished it first, we report what really happened.
      ({ job, nowMs, serving } = await readJob(jobId));
      // The record can only vanish in a rare race (its TTL ran out right now).
      if (!job) return respond(res, 404, errorBody("no such job"), { "Server-Timing": serverTiming([["total", performance.now() - t0]]) });
    }
    // Time after step 2/3.
    const t2 = performance.now();

    // Build the answer. Status and place in line (0 once the job has ended).
    const status = job.st;
    const out = { status, tier: Number(job.tr), position: 0 };
    if (status === "queued" || status === "processing") {
      // Place in line = ticket - now serving of its tier (at least 1).
      out.position = Math.max(1, Number(job.tk) - (serving.get(Number(job.tr)) || 0));
    } else {
      // Ended: how long it waited and how long the agent took.
      out.queue_wait_ms = Number(job.qw || 0);
      out.processing_ms = Number(job.pm || 0);
      // The agent's answer, or why it failed.
      out[status === "done" ? "answer" : "error"] = job.a || "";
    }
    const timing = [["redis", t2 - t1], ["total", t2 - t0]];
    return respond(res, 200, JSON.stringify(out), { "Server-Timing": serverTiming(timing) });
  }

  // --- Routing -------------------------------------------------------------------

  return async function handle(req, res) {
    // Start the clock for this request.
    const t0 = performance.now();
    // Path without the query string, e.g. "/node/chat".
    let p = req.url.split("?")[0];
    // Strip the language prefix: "/node/chat" -> "/chat".
    if (p.startsWith(PATH_PREFIX)) p = p.slice(PATH_PREFIX.length) || "/";
    // Health check: no user id, no Redis, no SQS.
    if (p === "/healthz") return respond(res, 200, '{"ok":true}');

    // Which route is this? Anything else is a 404.
    const isChat = p === "/chat";
    const isResult = p.startsWith("/result/");
    const total = () => ({ "Server-Timing": serverTiming([["total", performance.now() - t0]]) });
    if (!(isChat || isResult)) return respond(res, 404, errorBody("not found"), total());
    // Each route allows exactly one method.
    if (req.method !== (isChat ? "POST" : "GET")) return respond(res, 405, errorBody("method not allowed"), total());

    // The caller must say who they are (test only: callers can fake this header).
    const user = req.headers["x-user-id"];
    // Missing or unsafe characters -> 400.
    if (typeof user !== "string" || !CALLER_ID.test(user)) {
      return respond(res, 400, errorBody("X-User-Id header is required (letters, digits, _ . :)"), total());
    }

    // Hand over to the route.
    if (isChat) return chat(req, res, user, t0);
    return result(res, user, p.slice("/result/".length), t0);
  };
}

// --- Start ----------------------------------------------------------------------

// Write the test-only seed users into the tiers hash (HSET is safe to repeat).
async function seedTiers(redis, rules) {
  // user id -> tier number.
  const seed = rules.cfg.seed;
  if (Object.keys(seed).length) {
    // One call writes all users: field = user id, value = tier.
    await redis.hset("tiers", seed);
    console.log(`seeded ${Object.keys(seed).length} test users into the tiers hash`);
  }
}

// Start everything.
async function main() {
  // Redis connection (reused by all requests) with the three Lua scripts attached.
  const redis = defineScripts(new Redis(REDIS_URL), ["accept", "finish", "pollLimit"]);
  // Load rules before taking traffic.
  const rules = new Rules();
  await rules.loadInitial();
  // TEST ONLY: write the seed users into the tiers hash.
  await seedTiers(redis, rules);
  // With S3, keep checking for rule changes (and re-apply the seed users after one).
  if (rules.s3) rules.startRefresh(() => seedTiers(redis, rules));
  // SQS client: signs calls with the instance role. Short timeouts so a slow
  // SQS cannot hold requests for long.
  const sqs = new SQSClient({ requestHandler: { connectionTimeout: 2000, requestTimeout: 5000 } });
  // HTTP server using our handler. A failed request becomes a 500, never a crash.
  const handler = makeHandler({ redis, sqs, rules });
  const server = http.createServer((req, res) => {
    handler(req, res).catch((err) => {
      console.error("request failed:", err);
      if (!res.headersSent) respond(res, 500, errorBody("internal error"));
      else res.end();
    });
  });
  // Default is loopback. The Dockerfile sets HOST=0.0.0.0 because inside the
  // container only the Docker network can reach it (no port is published).
  const host = env.HOST || "127.0.0.1";
  const port = Number(env.PORT || "8000");
  server.listen(port, host, () => console.log(`listening on ${host}:${port}`));
  // docker stop sends SIGTERM; Node as PID 1 would otherwise ignore it and be killed after 10 s.
  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, () => {
      console.log("limiter stopped");
      process.exit(0);
    });
  }
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
module.exports = { makeHandler, seedTiers, readBody, serverTiming };
