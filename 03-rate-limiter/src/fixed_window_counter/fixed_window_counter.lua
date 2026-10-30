--[[
Fixed window counter rate limiter, run atomically inside Redis.
Checks one or more counters per request, all-or-nothing.

WHERE THIS FITS
  1. A request hits a limiter server (Python / Go / Node).
  2. The server finds the matching rules (in memory) and works out who the
     caller is: user id, session cookie, and IP (see docs/architecture.md,
     "Identifying the caller").
  3. For each matching rule it builds a base key "<rule>-u<caller>",
     e.g. "user-u42" and "ip-u3405803783", and asks Redis to run THIS
     script on all of those keys in one call.
  4. This script returns allowed / denied.
  5. The server forwards to the backend (allowed) or returns 429 (denied).

ALL-OR-NOTHING
  A request is allowed only if EVERY counter is under its limit. If any
  counter says no, NO counter is increased.

HOW REDIS FINDS THE COUNTER
  The key IS the address: one direct hash-table hit, no search. This script
  adds the window number to the base key:

    user-u42-w1761580000        -> "7"   (7 requests in this 1 s window)
    ip-u3405803783-w1761580000  -> "12"

  Key names are built here from the arguments. That is fine on one Redis
  server; Redis Cluster would need hash tags so all keys land on one node.

WHY LUA INSIDE REDIS
  Redis runs a script start to finish without running any other command in
  between. Read -> check -> count is one step, so two requests can never
  both take the last place in a window.

HOW THE FIXED WINDOW WORKS
  - Time is cut into windows of `window_s` seconds: window number =
    floor(now_ms / window_ms), from the Redis clock.
  - Each window has its own counter. A request adds 1. Over `limit` -> denied.
  - The counter deletes itself when its window ends (plus a small grace).
  - Known weakness: up to 2 x limit can pass around a window boundary
    (limit at the end of one window + limit at the start of the next).

CALL IT WITH (N counters)
  EVALSHA <sha> N <key1> .. <keyN>  <limit1> <window_s1> .. <limitN> <window_sN>

  KEYS[i]          base key i, e.g. "user-u42"
  ARGV[2i-1]       limit for key i      (requests per window, whole number >= 1)
  ARGV[2i]         window_s for key i   (seconds, > 0)

RETURNS 4 integers
  [1] allowed         1 = allowed, 0 = denied
  [2] remaining       requests left in the fullest window
  [3] retry_after_ms  0 if allowed; else ms until the denying window ends
                      (largest over the denying counters)
  [4] denied_by       0 if allowed; else the number (1..N) of the first
                      counter that said no
]]

-- How many counters this request must pass.
local n = #KEYS
-- At least one counter is required.
if n == 0 then
  -- Send an error back to the server; the script stops here.
  return redis.error_reply("need at least one key")
end
-- Each key needs its own limit and window.
if #ARGV ~= n * 2 then
  return redis.error_reply("need 2 args (limit, window_s) per key")
end

-- "Now" from the Redis server clock: {seconds, microseconds}.
local t = redis.call("TIME")
-- Milliseconds since 1970 (the same "now" for every counter).
local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

-- Pass 1: read every counter and decide. Nothing is written yet.
-- Working copy of each counter.
local counters = {}
-- Assume allowed until a counter says no.
local allowed = 1
-- Longest wait across the counters that said no.
local retry_after_ms = 0
-- Which counter said no first (0 = none).
local denied_by = 0

-- Visit each counter in turn.
for i = 1, n do
  -- Settings for this key; Redis passes strings, so convert.
  local limit = tonumber(ARGV[2 * i - 1])
  local window_s = tonumber(ARGV[2 * i])
  -- Reject bad settings with a clear error.
  if not limit or limit < 1 or limit ~= math.floor(limit) then
    return redis.error_reply("limit must be a whole number >= 1 (" .. KEYS[i] .. ")")
  end
  -- A window must have a length.
  if not window_s or window_s <= 0 then
    return redis.error_reply("window_s must be a number > 0 (" .. KEYS[i] .. ")")
  end
  -- Window length in ms (at least 1).
  local window_ms = math.max(1, math.floor(window_s * 1000 + 0.5))
  -- Which window "now" falls in.
  local index = math.floor(now_ms / window_ms)
  -- This window's key, e.g. "user-u42-w1761580000". "%.0f" writes every digit.
  local key = KEYS[i] .. "-w" .. string.format("%.0f", index)
  -- Requests already counted in this window (missing key = 0).
  local count = tonumber(redis.call("GET", key)) or 0
  -- Milliseconds until this window ends.
  local ends_in_ms = (index + 1) * window_ms - now_ms

  -- No room for one more?
  if count + 1 > limit then
    -- The whole request is denied.
    allowed = 0
    -- Remember the first counter that said no.
    if denied_by == 0 then
      denied_by = i
    end
    -- Keep the longest wait.
    if ends_in_ms > retry_after_ms then
      retry_after_ms = ends_in_ms
    end
  end

  -- Save the working copy for pass 2.
  counters[i] = { key = key, count = count, limit = limit, ends_in_ms = ends_in_ms }
end

-- Pass 2: count the request only if every counter agreed.
-- Fewest requests left over all counters (nil = not set yet).
local remaining = nil
-- Visit each counter again.
for i = 1, n do
  -- The working copy from pass 1.
  local c = counters[i]
  -- Allowed: add 1 and delete the key when the window ends (+1 s grace).
  if allowed == 1 then
    c.count = redis.call("INCR", c.key)
    redis.call("PEXPIRE", c.key, c.ends_in_ms + 1000)
  end
  -- Requests left in this window (never below 0).
  local left = math.max(0, c.limit - c.count)
  -- Track the smallest.
  if remaining == nil or left < remaining then
    remaining = left
  end
end

-- Answer the server (whole numbers only).
return { allowed, remaining, retry_after_ms, denied_by }
