--[[
Leaky bucket (queue) "door guard", run atomically inside Redis.
Decides whether a new chat request may join the line, and if so takes a
ticket for it. It does NOT control the speed: the worker does (see
docs/leaky_bucket/README.md). Redis only counts who is waiting and keeps the bookkeeping.

WHERE THIS FITS
  1. A request hits a limiter server (Python / Go / Node): POST /<lang>/chat.
  2. The server makes a random job id and calls THIS script.
  3. Accepted -> the server sends the job to the SQS FIFO queue of the
     user's tier and answers 202. Full line -> the server answers 429.
  4. Later the worker finishes the job with finish.lua.

WHAT IT DOES, IN ONE ATOMIC STEP
  a) Looks up the user's tier in the hash "tiers" (user id -> small number).
     Missing, not a whole number, or not configured -> tier 0.
  b) Reads how many requests this user already has waiting ("line count")
     and compares it with that tier's max_waiting.
  c) Full -> stop (nothing is written). Room -> line count +1, take the next
     ticket of this language and tier, and create the job record.

KEYS IT USES (short names save memory; <lang> = python / go / node)
  tiers                 hash: user id -> tier number          (set elsewhere)
  lq-<lang>-u<user>     line count: this user's waiting jobs  (TTL, see below)
  tk-<lang>-<tier>      last ticket handed out in this tier's line
  sv-<lang>-<tier>      last ticket finished ("now serving"); finish.lua moves it
  job-<uuid>            hash with the job's status, times and answer (TTL)

  Position in line = ticket - now serving. 1 means "next up".

CLUSTER NOTE
  The script builds lq-.. / tk-.. / sv-.. key names from its arguments
  instead of receiving them in KEYS. That is fine on one Redis node, which is
  what this project runs. Redis Cluster needs every key of one script call
  in the same slot, so a later phase would add a hash tag, e.g.
  "lq-{python}-u42", "tk-{python}-1" (same {python} in all keys of one call).

CALL IT WITH
  EVALSHA <sha> 1 tiers
          <lang> <user> <job_id> <line_ttl_s> <job_ttl_s>
          <tier_1> <max_waiting_1> <interval_ms_1>  ... (3 numbers per tier)

  KEYS[1]     the tiers hash name ("tiers")
  ARGV[1]     lang          python | go | node
  ARGV[2]     user          validated user id (letters, digits, _ . :)
  ARGV[3]     job_id        random UUID made by the server
  ARGV[4]     line_ttl_s    seconds the line count lives (about the SQS
                            retention plus a cushion; a crashed worker can
                            then never lock a user out for long)
  ARGV[5]     job_ttl_s     seconds the job record lives (5 minutes)
  ARGV[6..]   one group of 3 per configured tier, from rules.yaml:
                tier, max_waiting (jobs per user), interval_ms (1000 / leak_rate)
              Tier 0 must be present: it is the fallback.

RETURNS 5 integers
  [1] accepted        1 = joined the line, 0 = line full
  [2] tier            the tier that was applied (after the fallback)
  [3] ticket          the job's ticket (0 if not accepted)
  [4] position        place in line, 1 = next up (0 if not accepted)
  [5] retry_after_ms  0 if accepted; if full, one leak interval of the tier,
                      the soonest a place can free up
]]

-- ---------------------------------------------------------------------------
-- 1. Check the call shape.
-- ---------------------------------------------------------------------------

-- Exactly one key (the tiers hash) is expected.
if #KEYS ~= 1 then
  -- Send an error back to the server; the script stops here.
  return redis.error_reply("need exactly 1 key (the tiers hash)")
end

-- 5 fixed arguments plus 3 per tier, and at least one tier.
if #ARGV < 8 or (#ARGV - 5) % 3 ~= 0 then
  return redis.error_reply("need 5 args + (tier, max_waiting, interval_ms) per tier")
end

-- ---------------------------------------------------------------------------
-- 2. Read the arguments.
-- ---------------------------------------------------------------------------

-- Language of the limiter that called us; part of every key name.
local lang = ARGV[1]
-- The user asking to join the line.
local user = ARGV[2]
-- The new job's id.
local job_id = ARGV[3]
-- Lifetimes in seconds (whole numbers).
local line_ttl = tonumber(ARGV[4])
local job_ttl = tonumber(ARGV[5])

-- Reject bad lifetimes with a clear error instead of misbehaving.
if not line_ttl or line_ttl <= 0 or not job_ttl or job_ttl <= 0 then
  return redis.error_reply("line_ttl_s and job_ttl_s must be numbers > 0")
end

-- Per-tier settings, indexed by tier number: cfg[1] = { max = 10, interval = 200 }.
local cfg = {}
-- Walk the groups of 3 that follow the 5 fixed arguments.
for i = 6, #ARGV, 3 do
  -- Tier number this group is about.
  local tier_id = tonumber(ARGV[i])
  -- Most jobs one user may have waiting in this tier.
  local max_waiting = tonumber(ARGV[i + 1])
  -- Milliseconds between two starts in this tier (1000 / leak_rate).
  local interval_ms = tonumber(ARGV[i + 2])
  -- All three must be numbers, and the limits must make sense.
  if not tier_id or not max_waiting or max_waiting < 1 or not interval_ms or interval_ms <= 0 then
    return redis.error_reply("bad tier settings starting at ARGV[" .. i .. "]")
  end
  -- Remember them.
  cfg[tier_id] = { max = max_waiting, interval = interval_ms }
end

-- Tier 0 is the fallback for unknown users and unknown tiers.
if cfg[0] == nil then
  return redis.error_reply("tier 0 must be configured")
end

-- ---------------------------------------------------------------------------
-- 3. Find the user's tier.
-- ---------------------------------------------------------------------------

-- One hash lookup: the user id is the field. Returns false if not set.
local tier = tonumber(redis.call("HGET", KEYS[1], user))
-- Not set, not a number, fractional, or a tier we have no settings for -> tier 0.
if tier == nil or tier ~= math.floor(tier) or cfg[tier] == nil then
  tier = 0
end
-- The settings that apply to this user.
local tier_cfg = cfg[tier]

-- ---------------------------------------------------------------------------
-- 4. Is there room in this user's line?
-- ---------------------------------------------------------------------------

-- This user's line count key, e.g. "lq-python-u42".
local line_key = "lq-" .. lang .. "-u" .. user
-- How many jobs this user has waiting now (0 if the key doesn't exist).
local waiting = tonumber(redis.call("GET", line_key)) or 0

-- Line full for this user: nothing is written, the request is turned away.
if waiting >= tier_cfg.max then
  -- accepted=0, tier, no ticket, no position, retry after one leak interval.
  return { 0, tier, 0, 0, math.ceil(tier_cfg.interval) }
end

-- ---------------------------------------------------------------------------
-- 5. Join the line: count, ticket, job record.
-- ---------------------------------------------------------------------------

-- One more waiting job for this user.
redis.call("INCR", line_key)
-- Refresh the safety timer: if nothing ever releases this count, it clears itself.
redis.call("EXPIRE", line_key, math.ceil(line_ttl))

-- Take the next ticket of this language and tier (INCR creates the key at 1).
local ticket = redis.call("INCR", "tk-" .. lang .. "-" .. tier)
-- The last finished ticket of this line (0 if nothing finished yet).
local serving = tonumber(redis.call("GET", "sv-" .. lang .. "-" .. tier)) or 0
-- Place in line. max(1, ..) guards against counters that drifted apart.
local position = math.max(1, ticket - serving)

-- "Now" from the Redis server clock, so every container agrees on time.
local t = redis.call("TIME")
-- Milliseconds since 1970 (seconds * 1000 + microseconds / 1000, rounded down).
local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

-- The job record. Short field names:
--   st status, tr tier, tk ticket, u user, lg language, t0 accepted at (ms).
-- More fields are added later by finish.lua (t1, t2, a, qw, pm).
redis.call("HSET", "job-" .. job_id,
  "st", "queued", "tr", tier, "tk", ticket, "u", user, "lg", lang, "t0", now_ms)
-- The record disappears by itself, so Redis never fills up with old jobs.
redis.call("EXPIRE", "job-" .. job_id, math.ceil(job_ttl))

-- ---------------------------------------------------------------------------
-- 6. Answer the server (whole numbers only).
-- ---------------------------------------------------------------------------
return { 1, tier, ticket, position, 0 }
