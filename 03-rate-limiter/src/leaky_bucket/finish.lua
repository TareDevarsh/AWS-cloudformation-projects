--[[
Leaky bucket (queue) job bookkeeping, run atomically inside Redis.
The counterpart of accept.lua: it moves a job through its life and, when the
job ends, gives the user's place in line back.

WHO CALLS IT
  - The worker, when it STARTS a job          -> status "processing"
  - The worker, when the backend answered     -> status "done"
  - The worker, after the last failed attempt -> status "failed"
  - A limiter, when the SQS message is gone   -> status "expired"
    (SQS dropped it after the 2 minute retention; found out on a poll)
  - A limiter, if it could not send to SQS    -> status "failed"

  "processing" is a step, not an end: nothing is released. The other three
  END the job.

ENDING A JOB (done / failed / expired), ALL IN ONE ATOMIC STEP
  1. Save status, answer or error text, and the times.
  2. Line count of the user -1, but never below 0 (it may have expired).
  3. "Now serving" of the job's tier +1, so everyone behind moves up.
  4. Restart the job record's timer, so the client has the full time to
     read the result.

SAFE TO CALL TWICE
  SQS can deliver a message again (slow worker, crash), and a poll can race
  the worker. An ended job is never changed again, so the line count and
  "now serving" move exactly once per job.

CLUSTER NOTE
  The user's line count and the tier counters are named from fields stored in
  the job record, not passed in KEYS. Fine on one Redis node (this project).
  Redis Cluster would need a hash tag in all of them (see accept.lua).

CALL IT WITH
  EVALSHA <sha> 1 job-<uuid>  <status> <text> <job_ttl_s>

  KEYS[1]   the job record, "job-<uuid>"
  ARGV[1]   status    processing | done | failed | expired
  ARGV[2]   text      the answer (done) or the error (failed); "" otherwise
  ARGV[3]   job_ttl_s seconds the record lives from now (5 minutes)

RETURNS 1 integer
   1  the change was applied
   0  the job had already ended; nothing changed
  -1  there is no such job (its record expired)
]]

-- ---------------------------------------------------------------------------
-- 1. Check the call and read the job.
-- ---------------------------------------------------------------------------

-- Exactly one key (the job record) and three arguments.
if #KEYS ~= 1 or #ARGV ~= 3 then
  -- Send an error back to the caller; the script stops here.
  return redis.error_reply("need 1 key (job) and 3 args (status, text, job_ttl_s)")
end

-- The job record's key, e.g. "job-6f1c...".
local job_key = KEYS[1]
-- The status we are asked to set.
local status = ARGV[1]
-- Answer or error text.
local text = ARGV[2]
-- How long the record should live from now.
local job_ttl = tonumber(ARGV[3])

-- Only these four statuses are allowed.
if status ~= "processing" and status ~= "done" and status ~= "failed" and status ~= "expired" then
  return redis.error_reply("status must be processing, done, failed or expired")
end
-- A bad lifetime is a caller bug; say so.
if not job_ttl or job_ttl <= 0 then
  return redis.error_reply("job_ttl_s must be a number > 0")
end

-- Current status of the job (false if there is no such job).
local current = redis.call("HGET", job_key, "st")
-- No record: it expired (or never existed). Nothing to do.
if not current then
  return -1
end
-- Already ended: leave everything as it is (this is what makes repeats safe).
if current == "done" or current == "failed" or current == "expired" then
  return 0
end

-- ---------------------------------------------------------------------------
-- 2. "Now" from the Redis server clock, in milliseconds.
-- ---------------------------------------------------------------------------

-- TIME returns {seconds, microseconds within that second}.
local t = redis.call("TIME")
-- Milliseconds since 1970.
local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

-- ---------------------------------------------------------------------------
-- 3. "processing": just mark the start. Nothing is released yet.
-- ---------------------------------------------------------------------------

if status == "processing" then
  -- t1 = when this attempt started (overwritten by a retry; the last attempt wins).
  redis.call("HSET", job_key, "st", "processing", "t1", now_ms)
  -- Applied.
  return 1
end

-- ---------------------------------------------------------------------------
-- 4. done / failed / expired: end the job.
-- ---------------------------------------------------------------------------

-- When the job was accepted, and when its last attempt started.
local t0 = tonumber(redis.call("HGET", job_key, "t0")) or now_ms
-- A job that never started (expired, or failed before sending) has no t1: use "now".
local t1 = tonumber(redis.call("HGET", job_key, "t1")) or now_ms

-- Time spent waiting in line, in ms (never negative).
local queue_wait_ms = math.max(0, t1 - t0)
-- Time the last attempt took, in ms (0 if it never started).
local processing_ms = math.max(0, now_ms - t1)

-- Save the outcome. Short field names:
--   st status, t2 ended at (ms), a answer/error text, qw queue wait, pm processing.
redis.call("HSET", job_key,
  "st", status, "t2", now_ms, "a", text, "qw", queue_wait_ms, "pm", processing_ms)
-- Keep the record for the full lifetime counted from now.
redis.call("EXPIRE", job_key, math.ceil(job_ttl))

-- Who the job belongs to, and which line it stood in (stored by accept.lua).
local user = redis.call("HGET", job_key, "u")
local lang = redis.call("HGET", job_key, "lg")
local tier = redis.call("HGET", job_key, "tr")

-- Give the user's place back: line count -1, but never below 0.
local line_key = "lq-" .. lang .. "-u" .. user
-- 0 if the key already expired.
local waiting = tonumber(redis.call("GET", line_key)) or 0
-- Only decrement a positive count (a missing key would otherwise become -1).
if waiting > 0 then
  redis.call("DECR", line_key)
end

-- Move "now serving" up by one: everyone behind this job is one step closer.
redis.call("INCR", "sv-" .. lang .. "-" .. tier)

-- Applied.
return 1
