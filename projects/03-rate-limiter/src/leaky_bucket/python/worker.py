"""Leaky bucket (queue) worker, Python version.

The worker is the LEAK: it decides the speed. For every tier it runs one
pacing loop that starts one job every 1 / leak_rate seconds, no faster and
no slower (when there is work). Tier 0 (regular) starts 1 job per second,
tier 1 (business) starts 5 per second (see rules.yaml).

Job flow, per tier:
  1. Take jobs from the tier's SQS FIFO queue (a small batch at a time).
  2. At each tick START the next job. The worker does not wait for the
     previous reply, so a 200 ms agent still keeps up with 5 starts a second.
  3. For each started job (in the background):
       a) finish.lua "processing"        (status for the polling client)
       b) invoke the backend Lambda      (sleep_ms 200 = the fake chat agent)
       c) finish.lua "done"              (answer, times, line count -1, now serving +1)
       d) delete the SQS message
     Backend error: before the 5th attempt, hide the message for 1 s so SQS
     hands it back quickly. On the 5th failed attempt: finish.lua "failed" and
     delete the message, so nobody waits for a job that cannot succeed.

No bursts after idle time: a tick that was missed is not made up later.
After a start at time T the next start is no earlier than T + interval.

Config: see common.py, plus
  BACKEND_FUNCTION   Lambda function name; empty = built-in local echo agent
"""

# --- Imports ----------------------------------------------------------------

import asyncio  # async event loop: one pacing loop per tier plus background jobs
import json  # reads SQS message bodies and Lambda answers
import math  # ceil() for the visibility timeout of received batches
import os  # reads environment variables (config)
import signal  # stop cleanly on SIGTERM (docker stop)
from concurrent.futures import ThreadPoolExecutor  # threads for blocking boto3 calls

import boto3  # AWS SDK: SQS (take and delete jobs) and Lambda (the agent)
import redis.asyncio as aioredis  # async Redis client
from botocore.config import Config  # timeouts and retries for the SDK clients

# Shared helpers: config, rules, names.
from common import FINISH_LUA, JOB_TTL_S, LANGUAGE, REDIS_URL, Rules, job_key, log, queue_url, read_text

# --- Config (from environment variables, with local defaults) ---------------

# Backend Lambda name. Empty = local echo agent (testing).
BACKEND_FUNCTION = os.environ.get("BACKEND_FUNCTION", "")
# Seconds one receive call may wait for a message (long polling: returns early on arrival).
RECEIVE_WAIT_S = int(os.environ.get("RECEIVE_WAIT_S", "5"))
# Longest answer text we store in Redis, in characters.
MAX_ANSWER_CHARS = 2000
# Read timeout for SQS calls: longer than the long poll, or every empty receive would time out.
WAIT_READ_TIMEOUT = RECEIVE_WAIT_S + 5


class Backend:
    """The fake chat agent: the backend Lambda, or a local echo when BACKEND_FUNCTION is empty."""

    def __init__(self):
        # Lambda client only if a function is configured; signs calls with the instance role.
        # One try only (the queue does the retrying) and a read timeout above the agent's 200 ms.
        self.client = (boto3.client("lambda", config=Config(connect_timeout=3, read_timeout=15,
                                                             retries={"max_attempts": 1}))
                       if BACKEND_FUNCTION else None)

    def _invoke(self, payload):
        # Call the Lambda synchronously and wait for its answer.
        resp = self.client.invoke(FunctionName=BACKEND_FUNCTION, Payload=json.dumps(payload).encode())
        # The Lambda's response body.
        body = json.loads(resp["Payload"].read() or "{}")
        # FunctionError is set if the Lambda crashed: that is a failed attempt.
        if resp.get("FunctionError"):
            raise RuntimeError("backend error: " + str(body.get("errorMessage", "unknown")))
        # The agent's reply text.
        return str(body.get("reply", ""))

    async def call(self, payload):
        # No Lambda configured: act like the agent locally (testing).
        if self.client is None:
            # Take as long as the real agent would.
            await asyncio.sleep(payload["sleep_ms"] / 1000)
            # Same test hook as the real backend.
            if payload.get("fail"):
                raise RuntimeError("backend error: simulated failure")
            return "agent reply to: " + payload["message"]
        # boto3 is blocking, so run it in a worker thread.
        return await asyncio.to_thread(self._invoke, payload)


class Worker:
    def __init__(self, redis_client=None, sqs_client=None, backend=None):
        # Redis connection pool (connects on first use). Tests can pass a fake client.
        self.redis = redis_client or aioredis.from_url(REDIS_URL, decode_responses=True)
        # register_script sends EVALSHA and reloads the script if Redis lost it.
        self.finish = self.redis.register_script(read_text(FINISH_LUA))
        # SQS client: signs calls with the instance role. Tests can pass a client of their own.
        self.sqs = sqs_client or boto3.client("sqs", config=Config(connect_timeout=2, read_timeout=WAIT_READ_TIMEOUT))
        # The agent (Lambda or local echo). Tests can pass their own.
        self.backend = backend or Backend()
        # In-memory rules; the worker reads them once at start.
        self.rules = Rules()
        # Background jobs per tier id (one task per started job). Kept so tasks are not
        # garbage collected, and so a pacing loop knows if jobs of its tier are still in flight.
        self.running = {}

    async def run(self):
        # More threads than the default: long polls and Lambda calls each hold one while they wait.
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=32))
        # Load rules before starting (boto3/file IO in a worker thread).
        await asyncio.to_thread(self.rules.load_initial)
        # One pacing loop per tier. A tier added later needs a worker restart.
        loops = [asyncio.create_task(self.pace(t)) for t in self.rules.cfg["tiers"].values()]
        log.info("worker %s started: %s", LANGUAGE,
                 ", ".join(f'tier {t["id"]} {t["leak_rate"]}/s' for t in self.rules.cfg["tiers"].values()))
        try:
            # Run until a loop dies or we are cancelled.
            await asyncio.gather(*loops)
        finally:
            # Stop every loop and close Redis.
            for task in loops:
                task.cancel()
            await self.redis.aclose()

    # --- The pacing loop: this is the leak -----------------------------------

    async def pace(self, tier):
        # Settings of this tier.
        interval = tier["interval_ms"] / 1000
        # How many messages to take per receive: twice the leak rate, between 1 and 10.
        batch = min(10, max(1, int(2 * tier["leak_rate"])))
        # Received messages waiting for their tick, in queue order.
        buffer = []
        # Clock that never jumps (event loop time).
        loop = asyncio.get_running_loop()
        # Earliest time the next job may start.
        next_tick = loop.time()
        while True:
            # Nothing buffered: ask the queue.
            if not buffer:
                # SQS FIFO hands out no further message of the (single) group while earlier ones
                # are still being processed. So while jobs of this tier are in flight, check
                # again every 50 ms (short poll) to pick up the next batch the moment the last
                # delete lands. With nothing in flight, long poll: it returns as soon as a job arrives.
                busy = bool(self.running.get(tier["id"]))
                buffer = await self.receive(tier, batch, 0 if busy else RECEIVE_WAIT_S)
                # Empty answer: poll again (after a short pause if we were short polling).
                if not buffer:
                    if busy:
                        await asyncio.sleep(0.05)
                    continue
            # Wait for the tick. If we are already past it (idle time, slow receive)
            # there is no wait, and the missed ticks are NOT made up: no burst.
            await asyncio.sleep(max(0.0, next_tick - loop.time()))
            # The tick: start the next job in the background, without waiting for its reply.
            msg = buffer.pop(0)
            task = asyncio.create_task(self.handle(tier, msg))
            # Remember it until it ends, then forget it.
            jobs = self.running.setdefault(tier["id"], set())
            jobs.add(task)
            task.add_done_callback(jobs.discard)
            # Next start is one interval after THIS start (not after the old tick).
            next_tick = loop.time() + interval

    async def receive(self, tier, batch, wait_s):
        """Take up to `batch` messages from the tier's queue, waiting up to wait_s seconds (in a thread)."""
        # Queue settings.
        q = self.rules.cfg["queue"]
        # Messages wait in our buffer up to batch * interval; keep them hidden at least that long.
        hidden_s = q["visibility_timeout_s"] + math.ceil(batch * tier["interval_ms"] / 1000)
        try:
            resp = await asyncio.to_thread(
                self.sqs.receive_message, QueueUrl=queue_url(tier["id"]), MaxNumberOfMessages=batch,
                WaitTimeSeconds=wait_s, VisibilityTimeout=hidden_s,
                MessageSystemAttributeNames=["ApproximateReceiveCount"])
        except Exception:
            # SQS hiccup: log, pause briefly, and the loop tries again.
            log.exception("receive failed (tier %d)", tier["id"])
            await asyncio.sleep(1)
            return []
        # Messages come back in queue order (single message group).
        return resp.get("Messages", [])

    # --- One started job -----------------------------------------------------

    async def handle(self, tier, msg):
        # The queue URL, needed to delete or hide this message.
        url = queue_url(tier["id"])
        # How many times SQS has handed out this message (1 on the first try).
        attempt = int(msg.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
        try:
            # What the limiter put in the message: job id, chat text, test hook.
            body = json.loads(msg["Body"])
            job_id = body["job"]
        except (ValueError, KeyError):
            # Not one of ours: delete it so it cannot block the line.
            log.error("dropping malformed message %s", msg.get("MessageId"))
            return await self.delete(url, msg)
        key = job_key(job_id)
        q = self.rules.cfg["queue"]
        try:
            # a) Mark the start. 0 = already ended (a repeat delivery), -1 = record gone:
            #    in both cases there is nothing to do but remove the message.
            if await self.finish(keys=[key], args=["processing", "", JOB_TTL_S]) != 1:
                return await self.delete(url, msg)
            try:
                # b) Ask the agent. sleep_ms makes the Lambda take as long as a real model would.
                reply = await self.backend.call({"message": body.get("msg", ""), "fail": body.get("fail") is True,
                                                 "sleep_ms": self.rules.cfg["agent_sleep_ms"]})
            except Exception as e:
                # The agent failed. Last allowed attempt: end the job as failed and drop the message.
                if attempt >= q["max_attempts"]:
                    log.warning("job %s failed after %d attempts: %s", job_id, attempt, e)
                    await self.finish(keys=[key], args=["failed", f"failed after {attempt} attempts: {e}"[:MAX_ANSWER_CHARS], JOB_TTL_S])
                    return await self.delete(url, msg)
                # Otherwise hide the message for just 1 s so SQS returns it for a quick retry.
                log.info("job %s attempt %d failed: %s", job_id, attempt, e)
                await asyncio.to_thread(self.sqs.change_message_visibility, QueueUrl=url,
                                        ReceiptHandle=msg["ReceiptHandle"], VisibilityTimeout=q["retry_visibility_s"])
                return
            # c) Save the answer, give the user's place back, move "now serving" up.
            await self.finish(keys=[key], args=["done", reply[:MAX_ANSWER_CHARS], JOB_TTL_S])
            # d) Only now remove the message. If we crashed before this, SQS would redeliver it
            #    and step a) would see the job already ended.
            await self.delete(url, msg)
        except Exception:
            # Redis or SQS trouble: leave the message; SQS hands it back after its visibility timeout.
            log.exception("job %s: could not complete bookkeeping", job_id)

    async def delete(self, url, msg):
        # Remove the message from the queue for good.
        await asyncio.to_thread(self.sqs.delete_message, QueueUrl=url, ReceiptHandle=msg["ReceiptHandle"])


async def main():
    # The event loop and the worker.
    loop = asyncio.get_running_loop()
    worker = Worker()
    # Run the worker as a task so SIGTERM can cancel it.
    task = asyncio.create_task(worker.run())
    # docker stop sends SIGTERM; Python as PID 1 would otherwise ignore it and be killed after 10 s.
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        log.info("worker stopped")


# Run directly with "python worker.py".
if __name__ == "__main__":
    asyncio.run(main())
