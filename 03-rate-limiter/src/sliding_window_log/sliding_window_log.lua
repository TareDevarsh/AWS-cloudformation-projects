--[[
Sliding window log rate limiter, run atomically inside Redis.
Checks one or more logs per request, all-or-nothing.

WHERE THIS FITS
  1. A request hits a limiter server (Python / Go / Node).
  2. The server finds the matching rules (in memory) and works out who the
     caller is: user id, session cookie, and IP (see docs/architecture.md,
     "Identifying the caller").
  3. For each matching rule it builds a key "<rule>-u<caller>",
     e.g. "user-u42" and "ip-u3405803783", and asks Redis to run THIS
     script on all of those keys in one call, with a unique request id.
  4. This script returns allowed / denied.
  5. The server forwards to the backend (allowed) or returns 429 (denied).

ALL-OR-NOTHING
  A request is allowed only if EVERY log has room. If any log says no,
  NO log gets a new entry.

HOW REDIS FINDS THE LOG
  The key IS the address: one direct hash-table hit, no search. Each key
  is a sorted set: one entry per allowed request, scored by its time.

    user-u42  -> { "1761580000123456-9f2c..." : 1761580000123456, ... }

  Key names come from the server. Fine on one Redis server; Redis Cluster
  would need hash tags so all keys of one request land on one node.

WHY LUA INSIDE REDIS
  Redis runs a script start to finish without running any other command in
  between. Trim -> count -> add is one step, so two requests can never both
  take the last place.

HOW THE SLIDING LOG WORKS
  - The window is always "the last window_s seconds", measured from now.
  - Drop entries older than that, count what is left.
  - count + 1 <= limit -> add an entry (allowed). Else denied.
  - Exact: never more than `limit` in any window. Cost: one entry per
    allowed request, so memory grows with `limit`.
  - Entry name = time + request id, so two requests in the same
    microsecond are still two entries.

CALL IT WITH (N logs)
  EVALSHA <sha> N <key1> .. <keyN>  <request_id> <limit1> <window_s1> .. <limitN> <window_sN>

  KEYS[i]          log key i, e.g. "user-u42"
  ARGV[1]          unique request id (e.g. a uuid4 hex)
  ARGV[2i]         limit for key i      (requests per window, whole number >= 1)
  ARGV[2i+1]       window_s for key i   (seconds, > 0)

RETURNS 4 integers
  [1] allowed         1 = allowed, 0 = denied
  [2] remaining       requests left in the fullest log
  [3] retry_after_ms  0 if allowed; else ms until the oldest entry leaves
                      the window (largest over the denying logs)
  [4] denied_by       0 if allowed; else the number (1..N) of the first
                      log that said no
]]

-- How many logs this request must pass.
local n = #KEYS
-- At least one log is required.
if n == 0 then
  -- Send an error back to the server; the script stops here.
  return redis.error_reply("need at least one key")
end
-- One request id, then a limit and a window per key.
if #ARGV ~= 1 + n * 2 then
  return redis.error_reply("need a request id, then 2 args (limit, window_s) per key")
end

-- "Now" from the Redis server clock: {seconds, microseconds}.
local t = redis.call("TIME")
-- Microseconds since 1970 (the same "now" for every log).
local now_us = tonumber(t[1]) * 1000000 + tonumber(t[2])
-- Entry name: time + request id, unique even within one microsecond.
local member = string.format("%.0f", now_us) .. "-" .. ARGV[1]

-- Pass 1: trim and count every log, and decide. Nothing is added yet.
-- Working copy of each log.
local logs = {}
-- Assume allowed until a log says no.
local allowed = 1
-- Longest wait across the logs that said no.
local retry_after_ms = 0
-- Which log said no first (0 = none).
local denied_by = 0

-- Visit each log in turn.
for i = 1, n do
  -- This log's key, e.g. "user-u42".
  local key = KEYS[i]
  -- Settings for this key; Redis passes strings, so convert.
  local limit = tonumber(ARGV[2 * i])
  local window_s = tonumber(ARGV[2 * i + 1])
  -- Reject bad settings with a clear error.
  if not limit or limit < 1 or limit ~= math.floor(limit) then
    return redis.error_reply("limit must be a whole number >= 1 (" .. key .. ")")
  end
  -- A window must have a length.
  if not window_s or window_s <= 0 then
    return redis.error_reply("window_s must be a number > 0 (" .. key .. ")")
  end
  -- Window length in microseconds.
  local window_us = math.floor(window_s * 1000000 + 0.5)

  -- Drop entries that are no longer inside the last window_us.
  redis.call("ZREMRANGEBYSCORE", key, "-inf", string.format("%.0f", now_us - window_us))
  -- Requests still inside the window.
  local count = redis.call("ZCARD", key)

  -- No room for one more?
  if count + 1 > limit then
    -- The whole request is denied.
    allowed = 0
    -- Remember the first log that said no.
    if denied_by == 0 then
      denied_by = i
    end
    -- The oldest entry: {member, score}.
    local oldest = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")
    -- When it leaves the window, there is room again (ms, rounded up).
    local wait_ms = math.ceil((tonumber(oldest[2]) + window_us - now_us) / 1000)
    -- Keep the longest wait.
    if wait_ms > retry_after_ms then
      retry_after_ms = wait_ms
    end
  end

  -- Save the working copy for pass 2.
  logs[i] = { key = key, count = count, limit = limit, window_us = window_us }
end

-- Pass 2: add the request only if every log agreed.
-- Fewest requests left over all logs (nil = not set yet).
local remaining = nil
-- Visit each log again.
for i = 1, n do
  -- The working copy from pass 1.
  local l = logs[i]
  -- Allowed: add this request, and delete the log one window after it.
  if allowed == 1 then
    redis.call("ZADD", l.key, string.format("%.0f", now_us), member)
    redis.call("PEXPIRE", l.key, math.ceil(l.window_us / 1000))
    -- One more entry in the log now.
    l.count = l.count + 1
  end
  -- Requests left in this log (never below 0).
  local left = math.max(0, l.limit - l.count)
  -- Track the smallest.
  if remaining == nil or left < remaining then
    remaining = left
  end
end

-- Answer the server (whole numbers only).
return { allowed, remaining, retry_after_ms, denied_by }
