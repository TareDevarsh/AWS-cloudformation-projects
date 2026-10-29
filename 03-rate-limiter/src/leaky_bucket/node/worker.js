// Leaky bucket (queue) worker, Node version. Same behaviour as python/worker.py.
//
// The worker is the LEAK: it decides the speed. For every tier it runs one
// pacing loop that starts one job every 1 / leak_rate seconds, no faster and
// no slower (when there is work). Tier 0 (regular) starts 1 job per second,
// tier 1 (business) starts 5 per second (see rules.yaml).
//
// Job flow, per tier:
//   1. Take jobs from the tier's SQS FIFO queue (a small batch at a time).
//   2. At each tick START the next job. The worker does not wait for the
//      previous reply, so a 200 ms agent still keeps up with 5 starts a second.
//   3. For each started job (in the background):
//        a) finish.lua "processing"        (status for the polling client)
//        b) invoke the backend Lambda      (sleep_ms 200 = the fake chat agent)
//        c) finish.lua "done"              (answer, times, line count -1, now serving +1)
//        d) delete the SQS message
//      Backend error: before the 5th attempt, hide the message for 1 s so SQS
//      hands it back quickly. On the 5th failed attempt: finish.lua "failed" and
//      delete the message, so nobody waits for a job that cannot succeed.
//
// No bursts after idle time: a tick that was missed is not made up later.
// After a start at time T the next start is no earlier than T + interval.
//
// Config: see common.js, plus
//   BACKEND_FUNCTION   Lambda function name; empty = built-in local echo agent
//   RECEIVE_WAIT_S     long poll seconds when nothing is in flight (default 5)

"use strict"; // stricter JavaScript: catches common mistakes

// --- Imports -----------------------------------------------------------------

const Redis = require("ioredis"); // Redis client
const { SQSClient, ReceiveMessageCommand, DeleteMessageCommand,
  ChangeMessageVisibilityCommand } = require("@aws-sdk/client-sqs"); // takes and deletes jobs
const { LambdaClient, InvokeCommand } = require("@aws-sdk/client-lambda"); // invokes the agent
// Shared helpers: config, rules, scripts, names.
const { LANGUAGE, REDIS_URL, JOB_TTL_S, Rules, defineScripts, jobKey, queueUrl } = require("./common");

// --- Config (from environment variables, with local defaults) ----------------

// Shortcut to environment variables.
const env = process.env;
// Backend Lambda name. Empty = local echo agent (testing).
const BACKEND_FUNCTION = env.BACKEND_FUNCTION || "";
// Seconds one receive call may wait for a message (long polling: returns early on arrival).
const RECEIVE_WAIT_S = Number(env.RECEIVE_WAIT_S || "5");
// Longest answer text we store in Redis, in characters.
const MAX_ANSWER_CHARS = 2000;
// Read timeout for SQS calls: longer than the long poll, or every empty receive would time out.
const WAIT_READ_TIMEOUT_MS = (RECEIVE_WAIT_S + 5) * 1000;

// Pause for `ms` milliseconds without blocking other work.
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// --- The fake chat agent -------------------------------------------------------

// The backend Lambda, or a local echo when BACKEND_FUNCTION is empty.
class Backend {
  constructor() {
    // Lambda client only if a function is configured; signs calls with the instance role.
    // One try only (the queue does the retrying) and a read timeout above the agent's 200 ms.
    this.client = BACKEND_FUNCTION
      ? new LambdaClient({ maxAttempts: 1, requestHandler: { connectionTimeout: 3000, requestTimeout: 15000 } })
      : null;
  }

  // Ask the agent for a reply; throws on any failure.
  async call(payload) {
    // No Lambda configured: act like the agent locally (testing).
    if (!this.client) {
      // Take as long as the real agent would.
      await sleep(payload.sleep_ms);
      // Same test hook as the real backend.
      if (payload.fail) throw new Error("backend error: simulated failure");
      return `agent reply to: ${payload.message}`;
    }
    // Call the Lambda synchronously and wait for its answer.
    const resp = await this.client.send(new InvokeCommand({
      FunctionName: BACKEND_FUNCTION, // which Lambda
      Payload: Buffer.from(JSON.stringify(payload)), // the request, as bytes
    }));
    // The Lambda's response body (empty = {}).
    const text = Buffer.from(resp.Payload || []).toString() || "{}";
    const body = JSON.parse(text);
    // FunctionError is set if the Lambda crashed: that is a failed attempt.
    if (resp.FunctionError) throw new Error(`backend error: ${(body && body.errorMessage) || "unknown"}`);
    // The agent's reply text.
    return String((body && body.reply) ?? "");
  }
}

// --- The worker ----------------------------------------------------------------

class Worker {
  // Parts can be replaced by tests: redis (with .finish), sqs (with .send), backend (with .call), rules.
  constructor({ redis, sqs, backend, rules }) {
    // Redis connection pool (connects on first use) with finish.lua attached.
    this.redis = redis;
    // SQS client: signs calls with the instance role.
    this.sqs = sqs;
    // The agent (Lambda or local echo).
    this.backend = backend;
    // In-memory rules; the worker reads them once at start.
    this.rules = rules;
    // Jobs in flight per tier id (one promise per started job), so a pacing loop knows
    // if jobs of its tier are still being processed.
    this.running = new Map();
    // Set to true to make every loop end.
    this.stopping = false;
  }

  // Start one pacing loop per tier and run until stopped.
  async run() {
    // Load rules before starting.
    await this.rules.loadInitial();
    const tiers = [...this.rules.cfg.tiers.values()];
    console.log(`worker ${LANGUAGE} started: ` +
      tiers.map((t) => `tier ${t.id} ${t.leak_rate}/s`).join(", "));
    // One pacing loop per tier. A tier added later needs a worker restart.
    await Promise.all(tiers.map((t) => this.pace(t)));
  }

  // Ask every loop to end after its current step.
  stop() {
    this.stopping = true;
  }

  // --- The pacing loop: this is the leak -------------------------------------------

  async pace(tier) {
    // Settings of this tier.
    const interval = tier.interval_ms;
    // How many messages to take per receive: twice the leak rate, between 1 and 10.
    const batch = Math.min(10, Math.max(1, Math.trunc(2 * tier.leak_rate)));
    // Received messages waiting for their tick, in queue order.
    let buffer = [];
    // Earliest time (ms, clock that never jumps) the next job may start.
    let nextTick = performance.now();
    while (!this.stopping) {
      // Nothing buffered: ask the queue.
      if (buffer.length === 0) {
        // SQS FIFO hands out no further message of the (single) group while earlier ones
        // are still being processed. So while jobs of this tier are in flight, check
        // again every 50 ms (short poll) to pick up the next batch the moment the last
        // delete lands. With nothing in flight, long poll: it returns as soon as a job arrives.
        const busy = (this.running.get(tier.id) || new Set()).size > 0;
        buffer = await this.receive(tier, batch, busy ? 0 : RECEIVE_WAIT_S);
        // Empty answer: poll again (after a short pause if we were short polling).
        if (buffer.length === 0) {
          if (busy) await sleep(50);
          continue;
        }
      }
      // Wait for the tick. If we are already past it (idle time, slow receive)
      // there is no wait, and the missed ticks are NOT made up: no burst.
      await sleep(Math.max(0, nextTick - performance.now()));
      // The tick: start the next job in the background, without waiting for its reply.
      const msg = buffer.shift();
      const task = this.handle(tier, msg);
      // Remember it until it ends, then forget it.
      if (!this.running.has(tier.id)) this.running.set(tier.id, new Set());
      const jobs = this.running.get(tier.id);
      jobs.add(task);
      task.finally(() => jobs.delete(task));
      // Next start is one interval after THIS start (not after the old tick).
      nextTick = performance.now() + interval;
    }
  }

  // Take up to `batch` messages from the tier's queue, waiting up to waitS seconds.
  async receive(tier, batch, waitS) {
    // Queue settings.
    const q = this.rules.cfg.queue;
    // Messages wait in our buffer up to batch * interval; keep them hidden at least that long.
    const hiddenS = q.visibility_timeout_s + Math.ceil(batch * tier.interval_ms / 1000);
    try {
      const resp = await this.sqs.send(new ReceiveMessageCommand({
        QueueUrl: queueUrl(tier.id), // this language and tier
        MaxNumberOfMessages: batch, // up to this many
        WaitTimeSeconds: waitS, // long poll (0 = answer at once)
        VisibilityTimeout: hiddenS, // hidden from others while we hold them
        MessageSystemAttributeNames: ["ApproximateReceiveCount"], // how many times handed out
      }));
      // Messages come back in queue order (single message group).
      return resp.Messages || [];
    } catch (err) {
      // SQS hiccup: log, pause briefly, and the loop tries again.
      console.error(`receive failed (tier ${tier.id}):`, err.message);
      await sleep(1000);
      return [];
    }
  }

  // --- One started job ---------------------------------------------------------------

  async handle(tier, msg) {
    // The queue URL, needed to delete or hide this message.
    const url = queueUrl(tier.id);
    // How many times SQS has handed out this message (1 on the first try).
    const attempt = parseInt((msg.Attributes || {}).ApproximateReceiveCount || "1", 10);
    let body;
    let jobId;
    try {
      // What the limiter put in the message: job id, chat text, test hook.
      body = JSON.parse(msg.Body);
      jobId = body.job;
      if (typeof jobId !== "string") throw new Error("no job id");
    } catch (err) {
      // Not one of ours: delete it so it cannot block the line.
      console.error(`dropping malformed message ${msg.MessageId}`);
      return this.deleteMessage(url, msg).catch((e) => console.error("delete failed:", e.message));
    }
    const key = jobKey(jobId);
    const q = this.rules.cfg.queue;
    try {
      // a) Mark the start. 0 = already ended (a repeat delivery), -1 = record gone:
      //    in both cases there is nothing to do but remove the message.
      if ((await this.redis.finish(key, "processing", "", JOB_TTL_S)) !== 1) {
        return await this.deleteMessage(url, msg);
      }
      let reply;
      try {
        // b) Ask the agent. sleep_ms makes the Lambda take as long as a real model would.
        reply = await this.backend.call({
          message: body.msg ?? "", fail: body.fail === true, sleep_ms: this.rules.cfg.agent_sleep_ms,
        });
      } catch (e) {
        // The agent failed. Last allowed attempt: end the job as failed and drop the message.
        if (attempt >= q.max_attempts) {
          console.warn(`job ${jobId} failed after ${attempt} attempts: ${e.message}`);
          await this.redis.finish(key, "failed", `failed after ${attempt} attempts: ${e.message}`.slice(0, MAX_ANSWER_CHARS), JOB_TTL_S);
          return await this.deleteMessage(url, msg);
        }
        // Otherwise hide the message for just 1 s so SQS returns it for a quick retry.
        console.log(`job ${jobId} attempt ${attempt} failed: ${e.message}`);
        await this.sqs.send(new ChangeMessageVisibilityCommand({
          QueueUrl: url, ReceiptHandle: msg.ReceiptHandle, VisibilityTimeout: q.retry_visibility_s,
        }));
        return;
      }
      // c) Save the answer, give the user's place back, move "now serving" up.
      await this.redis.finish(key, "done", reply.slice(0, MAX_ANSWER_CHARS), JOB_TTL_S);
      // d) Only now remove the message. If we crashed before this, SQS would redeliver it
      //    and step a) would see the job already ended.
      await this.deleteMessage(url, msg);
    } catch (err) {
      // Redis or SQS trouble: leave the message; SQS hands it back after its visibility timeout.
      console.error(`job ${jobId}: could not complete bookkeeping:`, err.message);
    }
  }

  // Remove the message from the queue for good.
  async deleteMessage(url, msg) {
    await this.sqs.send(new DeleteMessageCommand({ QueueUrl: url, ReceiptHandle: msg.ReceiptHandle }));
  }
}

// --- Start -----------------------------------------------------------------------

async function main() {
  // Redis connection with finish.lua attached (the worker never uses the other scripts).
  const redis = defineScripts(new Redis(REDIS_URL), ["finish"]);
  // SQS client: signs calls with the instance role. Read timeout above the long poll.
  const sqs = new SQSClient({ requestHandler: { connectionTimeout: 2000, requestTimeout: WAIT_READ_TIMEOUT_MS } });
  const worker = new Worker({ redis, sqs, backend: new Backend(), rules: new Rules() });
  // docker stop sends SIGTERM; Node as PID 1 would otherwise ignore it and be killed after 10 s.
  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, () => {
      worker.stop();
      console.log("worker stopped");
      process.exit(0);
    });
  }
  await worker.run();
}

// Run only when started directly ("node worker.js"), not when imported by tests.
if (require.main === module) {
  main().catch((err) => {
    // Startup failed (bad rules, missing file): log and exit non-zero.
    console.error(err);
    process.exit(1);
  });
}

// Exported for tests.
module.exports = { Worker, Backend };
