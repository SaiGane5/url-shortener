"""Redis-backed distributed rate limiter (token bucket).

Why token bucket over a sliding window
--------------------------------------
A sliding-window log is more precise but stores one Redis entry per request
(ZSET member) -- O(requests) memory and an O(log n) trim on every call. A token
bucket keeps *two numbers* per client regardless of traffic, refills smoothly,
and naturally expresses "sustained 100/min but a burst of 100 is fine", which
is what a URL shortener actually wants: browsers open several links at once, so
a hard fixed window produces spurious 429s at window boundaries.

Why it is genuinely distributed
-------------------------------
State lives in Redis, not in the process. The read-modify-write happens inside
a Lua script, so it is atomic across replicas, and the refill clock is Redis's
own clock, so replica clock skew cannot leak tokens. Two requests from one
client that land on two different API containers share one bucket -- which is
exactly what docs/TESTING.md walks through verifying.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from redis.commands.core import AsyncScript
from redis.exceptions import RedisError

from app.cache.redis_client import RedisGateway
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import (
    rate_limit_decisions_total,
    rate_limit_evaluation_seconds,
    rate_limit_violations_total,
    redis_errors_total,
)
from app.ratelimit.policy import PolicyName, RateLimitPolicy, build_policies

logger = get_logger(__name__)

_SCRIPT_PATH: Final = Path(__file__).with_name("token_bucket.lua")
_TOKEN_BUCKET_SCRIPT: Final = _SCRIPT_PATH.read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Outcome of one limiter evaluation, plus everything the headers need."""

    allowed: bool
    policy: RateLimitPolicy
    remaining: int
    retry_after_seconds: int
    reset_after_seconds: int
    bucket: str
    #: True when Redis was unreachable and the policy's failure mode decided
    #: the outcome rather than the token bucket.
    degraded: bool = False

    def headers(self) -> dict[str, str]:
        """Standard-ish rate-limit response headers.

        `X-RateLimit-*` (the de-facto convention every HTTP client already
        understands) plus `Retry-After` on rejection, which is the only one
        browsers and well-behaved bots act on automatically.
        """
        headers = {
            "X-RateLimit-Limit": str(self.policy.requests),
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
            "X-RateLimit-Reset": str(self.reset_after_seconds),
            "X-RateLimit-Policy": self.policy.header_value,
            # Hashed client identity. Lets an operator verify the shared bucket
            # (`redis-cli HGETALL rl:v1:<policy>:<bucket>`) without the raw
            # client IP ever appearing in a header, a log, or a metric label.
            "X-RateLimit-Bucket": self.bucket,
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(self.retry_after_seconds, 1))
        if self.degraded:
            headers["X-RateLimit-Degraded"] = "true"
        return headers


class RateLimiter:
    """Evaluates token-bucket policies against Redis."""

    KEY_SCHEMA_VERSION: Final = 1

    def __init__(self, redis: RedisGateway, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings
        self._policies = build_policies(settings)
        # register_script uses EVALSHA and transparently re-uploads the body on
        # NOSCRIPT, so a Redis restart or failover does not need app awareness.
        self._script: AsyncScript = redis.client.register_script(_TOKEN_BUCKET_SCRIPT)

    @property
    def enabled(self) -> bool:
        return self._settings.rate_limit_enabled

    def policy(self, name: PolicyName) -> RateLimitPolicy:
        return self._policies[name]

    def bucket_id(self, identity: str) -> str:
        """Hash a client identity into a stable, non-reversible bucket id.

        The raw identity is a client IP. Hashing keeps IPs out of Redis keys,
        logs, and headers while remaining perfectly stable for limiting. A
        16-hex-character (64-bit) prefix is far beyond enough to avoid
        accidental collisions at any plausible client count.
        """
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

    def key(self, policy: RateLimitPolicy, bucket: str) -> str:
        prefix = self._settings.rate_limit_key_prefix
        return f"{prefix}:v{self.KEY_SCHEMA_VERSION}:{policy.name.value}:{bucket}"

    async def check(
        self,
        identity: str,
        policy_name: PolicyName,
        *,
        cost: int = 1,
    ) -> RateLimitDecision:
        """Consume `cost` tokens for `identity`. Never raises."""
        policy = self._policies[policy_name]
        bucket = self.bucket_id(identity)

        if not self.enabled:
            return RateLimitDecision(
                allowed=True,
                policy=policy,
                remaining=policy.burst,
                retry_after_seconds=0,
                reset_after_seconds=0,
                bucket=bucket,
            )

        started = time.perf_counter()
        try:
            raw = await self._script(
                keys=[self.key(policy, bucket)],
                args=[policy.burst, policy.refill_per_second, cost, policy.ttl_seconds],
                client=self._redis.client,
            )
        except (RedisError, OSError) as exc:
            redis_errors_total.labels(operation="rate_limit").inc()
            return self._degraded_decision(policy, bucket, exc)
        finally:
            rate_limit_evaluation_seconds.observe(time.perf_counter() - started)

        allowed = bool(int(raw[0]))
        decision = RateLimitDecision(
            allowed=allowed,
            policy=policy,
            remaining=int(raw[1]),
            retry_after_seconds=math.ceil(float(raw[2])),
            reset_after_seconds=math.ceil(float(raw[3])),
            bucket=bucket,
        )

        rate_limit_decisions_total.labels(
            policy=policy.name.value,
            decision="allowed" if allowed else "limited",
        ).inc()
        if not allowed:
            rate_limit_violations_total.labels(policy=policy.name.value).inc()

        return decision

    def _degraded_decision(
        self,
        policy: RateLimitPolicy,
        bucket: str,
        exc: Exception,
    ) -> RateLimitDecision:
        """Apply the policy's Redis-unavailable failure mode."""
        rate_limit_decisions_total.labels(
            policy=policy.name.value,
            decision="failed_open" if policy.fail_open else "failed_closed",
        ).inc()
        logger.error(
            "rate limiter unavailable; applying failure mode",
            extra={
                "policy": policy.name.value,
                "fail_open": policy.fail_open,
                "reason": type(exc).__name__,
            },
        )
        if not policy.fail_open:
            rate_limit_violations_total.labels(policy=policy.name.value).inc()
        return RateLimitDecision(
            allowed=policy.fail_open,
            policy=policy,
            remaining=0,
            retry_after_seconds=policy.window_seconds,
            reset_after_seconds=policy.window_seconds,
            bucket=bucket,
            degraded=True,
        )
