--[[
Token bucket rate limiter, run atomically inside Redis.
Checks one or more buckets per request, all-or-nothing.

WHERE THIS FITS
  1. A request hits a limiter server (Python / Go / Node).
  2. The server finds the matching rules (in memory) and works out who the
     caller is: user id, session cookie, and IP (see docs/architecture.md,
     "Identifying the caller").
  3. For each matching rule it builds a bucket key "<rule>-u<caller>",
     e.g. "user-u42" and "ip-u3405803783", and asks Redis to run THIS
     script on all of those keys in one call.
  4. This script returns allowed / denied.
  5. The server forwards to the backend (allowed) or returns 429 (denied).

ALL-OR-NOTHING
  A request is allowed only if EVERY bucket has enough tokens. If any
  bucket says no, NO bucket loses a token. (Otherwise a request blocked by
  the IP bucket would still use up the user's tokens.)

HOW REDIS FINDS THE USER'S BUCKET
  Redis is a key-value store. There is no query or search: the key IS the
  address. Each rule + caller gets its own key, so a lookup is one direct
  hash-table hit, no matter how many users exist.

    user-u42             -> { t = "7500",  ts = "1761580000123456" }
    cookie-u9f2c1a       -> { t = "5000",  ts = "1761580000456789" }
    ip-u3405803783       -> { t = "2000",  ts = "1761580000999999" }
                             (IP 203.0.113.7 stored as the number 3405803783)

MILLI-TOKENS
  Tokens are stored as whole numbers of milli-tokens (1 token = 1000).
  7.5 tokens is stored as 7500. Whole numbers are stored compactly by Redis
  and avoid floating-point rounding drift.
  Rules and return values still use normal tokens; only storage uses milli.

WHY LUA INSIDE REDIS
  Redis runs a script start to finish without running any other command in
  between. Read -> refill -> take -> write is one step, so two requests can
  never both take the last token, and every language shares this algorithm.

HOW THE BUCKET WORKS
  - Holds at most `bucket_size` tokens (max burst).
  - Gains `refill_rate` tokens per second (steady rate).
  - Each request takes `cost` tokens (usually 1). Not enough -> denied.
  - No timer adds tokens. On each request we work out how many tokens were
    earned since the last update ("lazy refill").

Needs Redis 5+ (uses TIME before writes, allowed since Redis 5).

CALL IT WITH (N buckets)
  EVALSHA <sha> N <key1> .. <keyN>  <size1> <rate1> <cost1> .. <sizeN> <rateN> <costN>

  KEYS[i]          bucket key i
  ARGV[3i-2]       bucket_size for key i   (tokens, > 0)
  ARGV[3i-1]       refill_rate for key i   (tokens per second, > 0)
  ARGV[3i]         cost for key i          (tokens, > 0, usually 1)

RETURNS 4 integers
  [1] allowed         1 = allowed, 0 = denied
  [2] remaining       whole tokens left in the emptiest bucket
  [3] retry_after_ms  0 if allowed; ms until every bucket has enough if
                      denied; -1 if a cost > its bucket_size (never succeeds)
  [4] denied_by       0 if allowed; else the number (1..N) of the first
                      bucket that said no, so the server can log which rule
]]

-- ---------------------------------------------------------------------------
-- 1. Check the call shape: N keys need exactly 3 numbers each.
-- ---------------------------------------------------------------------------

-- How many buckets this request must pass.
local n = #KEYS

-- At least one bucket is required.
if n == 0 then
  -- Send an error back to the server; the script stops here.
  return redis.error_reply("need at least one bucket key")
end

-- Each key needs its own size, rate and cost.
if #ARGV ~= n * 3 then
  return redis.error_reply("need 3 args (size, rate, cost) per key")
end

-- ---------------------------------------------------------------------------
-- 2. Get "now" once, from the Redis server clock.
--    Every limiter container uses the same clock, so they agree on time
--    even if their own machine clocks drift. All buckets use the same "now".
-- ---------------------------------------------------------------------------

-- TIME returns two strings: {seconds, microseconds within that second}.
local t = redis.call("TIME")

-- Combine both into one number: total microseconds since 1970.
local now_us = tonumber(t[1]) * 1000000 + tonumber(t[2])

-- ---------------------------------------------------------------------------
-- 3. Pass 1: for every bucket, read it, refill it, and see if it has enough.
--    Nothing is taken yet; we only decide.
-- ---------------------------------------------------------------------------

-- Working copy of each bucket after refill, filled in below.
local buckets = {}

-- Assume allowed until a bucket says no.
local allowed = 1

-- Longest wait across all buckets that said no (0 = no wait).
local retry_after_ms = 0

-- Which bucket said no first (0 = none).
local denied_by = 0

-- Visit each bucket in turn.
for i = 1, n do
  -- This bucket's key, e.g. "user-u42".
  local key = KEYS[i]

  -- This bucket's three settings. Redis passes strings, so convert.
  local bucket_size = tonumber(ARGV[3 * i - 2])
  local refill_rate = tonumber(ARGV[3 * i - 1])
  local cost = tonumber(ARGV[3 * i])

  -- Reject bad settings with a clear error instead of misbehaving.
  if not bucket_size or bucket_size <= 0 then
    return redis.error_reply("bucket_size must be a number > 0 (" .. key .. ")")
  end
  -- A rate of 0 would never refill (and we divide by it later).
  if not refill_rate or refill_rate <= 0 then
    return redis.error_reply("refill_rate must be a number > 0 (" .. key .. ")")
  end
  -- A request must cost something.
  if not cost or cost <= 0 then
    return redis.error_reply("cost must be a number > 0 (" .. key .. ")")
  end

  -- Capacity in milli-tokens, e.g. 10 tokens -> 10000 ("+0.5, floor" = round).
  local cap_m = math.floor(bucket_size * 1000 + 0.5)
  -- Cost in milli-tokens, e.g. 1 token -> 1000.
  local cost_m = math.floor(cost * 1000 + 0.5)
  -- Refill speed in milli-tokens per second. Can be fractional; never stored.
  local rate_m = refill_rate * 1000

  -- Read two fields of this bucket's hash in one call.
  -- Short field names ("t", "ts") save memory per user.
  local state = redis.call("HMGET", key, "t", "ts")
  -- Milli-tokens at the last update (nil if the bucket doesn't exist).
  local tokens_m = tonumber(state[1])
  -- Time of the last update in microseconds (nil if missing).
  local last_us = tonumber(state[2])

  -- No bucket yet: first-time caller, or an idle bucket that expired.
  if tokens_m == nil or last_us == nil then
    -- New callers start full, so they can burst right away.
    tokens_m = cap_m
    -- Treat the last update as "now", so no extra refill is added.
    last_us = now_us
  end

  -- Microseconds since the last update; max(0, ...) guards a clock step back.
  local elapsed_us = math.max(0, now_us - last_us)
  -- Whole milli-tokens earned in that time, rounded down.
  local earned_m = math.floor(elapsed_us * rate_m / 1000000)

  -- The timestamp we will save for this bucket.
  local new_ts
  if tokens_m + earned_m >= cap_m then
    -- Now full: cap it (extra tokens dropped = burst limit), restart clock.
    tokens_m = cap_m
    new_ts = now_us
  else
    -- Not full: add what was earned, and move the clock forward only by the
    -- time that paid for those whole milli-tokens. The leftover carries to
    -- the next request, so slow refill rates still refill under frequent
    -- requests instead of rounding down to 0 every time.
    tokens_m = tokens_m + earned_m
    new_ts = last_us + math.floor(earned_m * 1000000 / rate_m)
  end

  -- Not enough tokens in this bucket?
  if tokens_m < cost_m then
    -- The whole request is denied.
    allowed = 0
    -- Remember the first bucket that said no.
    if denied_by == 0 then
      denied_by = i
    end
    -- How long this bucket needs, in ms.
    local wait_ms
    if cost_m > cap_m then
      -- This bucket can never hold enough: waiting won't help.
      wait_ms = -1
    else
      -- Missing milli-tokens / milli-tokens per second, in ms, rounded up.
      wait_ms = math.ceil((cost_m - tokens_m) * 1000 / rate_m)
    end
    -- Keep the longest wait; "-1 = never" beats any number.
    if wait_ms == -1 or retry_after_ms == -1 then
      retry_after_ms = -1
    elseif wait_ms > retry_after_ms then
      retry_after_ms = wait_ms
    end
  end

  -- Save the working copy for pass 2.
  buckets[i] = { key = key, tokens_m = tokens_m, new_ts = new_ts,
                 cap_m = cap_m, cost_m = cost_m, rate_m = rate_m }
end

-- ---------------------------------------------------------------------------
-- 4. Pass 2: take tokens only if every bucket agreed, then save every bucket.
-- ---------------------------------------------------------------------------

-- Emptiest bucket after this request, in whole tokens (nil = not set yet).
local remaining = nil

-- Visit each bucket again.
for i = 1, n do
  -- The working copy from pass 1.
  local b = buckets[i]

  -- Take this bucket's cost only if the whole request is allowed.
  if allowed == 1 then
    b.tokens_m = b.tokens_m - b.cost_m
  end

  -- Save tokens and time. Saved on denials too, so the refill from pass 1
  -- isn't lost. "%.0f" writes every digit; tostring would shorten big
  -- numbers to "1.76158e+15" and lose precision.
  redis.call("HSET", b.key,
    "t", string.format("%.0f", b.tokens_m),
    "ts", string.format("%.0f", b.new_ts))

  -- Delete the bucket after it would be full again if left alone. A missing
  -- key already means "full", so this only frees memory.
  redis.call("PEXPIRE", b.key, math.ceil(b.cap_m * 1000 / b.rate_m))

  -- Whole tokens left in this bucket (milli / 1000, rounded down).
  local left = math.floor(b.tokens_m / 1000)
  -- Track the smallest.
  if remaining == nil or left < remaining then
    remaining = left
  end
end

-- ---------------------------------------------------------------------------
-- 5. Answer the server (whole numbers only; Redis turns Lua numbers into
--    integers).
-- ---------------------------------------------------------------------------
return { allowed, remaining, retry_after_ms, denied_by }
