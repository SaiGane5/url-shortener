-- Distributed token-bucket rate limiter.
--
-- Runs as a single Lua script so that read-modify-write of the bucket is
-- atomic from the point of view of every API replica: Redis executes scripts
-- one at a time, so two concurrent requests can never both observe the same
-- token count and both decrement it.
--
-- The clock comes from redis.call('TIME'), NOT from the caller. Every API
-- replica therefore refills against one shared clock, so node clock skew
-- cannot hand a client extra tokens. (Redis >= 5 uses effect-based script
-- replication, which makes non-deterministic commands like TIME safe here.)
--
-- KEYS[1] : bucket key
-- ARGV[1] : capacity        (max tokens = burst size)
-- ARGV[2] : refill_per_sec  (sustained rate)
-- ARGV[3] : cost            (tokens this request consumes)
-- ARGV[4] : ttl_seconds     (idle expiry, keeps the keyspace bounded)
--
-- Returns: { allowed(0|1), remaining_tokens(int), retry_after_sec(string),
--            reset_after_sec(string) }

local key            = KEYS[1]
local capacity       = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])
local cost           = tonumber(ARGV[3])
local ttl_seconds    = tonumber(ARGV[4])

local clock = redis.call('TIME')
local now = tonumber(clock[1]) + (tonumber(clock[2]) / 1000000)

local state      = redis.call('HMGET', key, 'tokens', 'updated_at')
local tokens     = tonumber(state[1])
local updated_at = tonumber(state[2])

if tokens == nil or updated_at == nil then
  -- First request in this window: start with a full bucket.
  tokens = capacity
  updated_at = now
end

-- Refill. Guard against a negative delta, which can only happen if the Redis
-- clock moved backwards (NTP step); treating it as zero elapsed time is the
-- conservative choice.
local elapsed = now - updated_at
if elapsed < 0 then
  elapsed = 0
end
tokens = math.min(capacity, tokens + (elapsed * refill_per_sec))

local allowed = 0
local retry_after = 0

if tokens >= cost then
  allowed = 1
  tokens = tokens - cost
else
  retry_after = (cost - tokens) / refill_per_sec
end

redis.call('HSET', key, 'tokens', tokens, 'updated_at', now)
redis.call('EXPIRE', key, ttl_seconds)

-- Seconds until the bucket is completely refilled.
local reset_after = (capacity - tokens) / refill_per_sec

-- Fractional values are returned as strings: Redis truncates Lua numbers to
-- integers when converting them to the RESP protocol.
return { allowed, math.floor(tokens), tostring(retry_after), tostring(reset_after) }
