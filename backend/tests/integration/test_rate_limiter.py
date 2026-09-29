"""Distributed token bucket, against a real Redis.

These tests are the reason the integration tier exists. The limiter's
correctness claim is "N API replicas share one budget, and concurrent requests
cannot over-admit", and that claim lives entirely in a Lua script executing
atomically inside Redis. A fake Redis would test our assumptions about Redis
rather than Redis itself, so everything here runs against the real server.
"""

from __future__ import annotations

import asyncio

import pytest

from app.cache.redis_client import RedisGateway
from app.core.config import Settings
from app.ratelimit.limiter import RateLimiter
from app.ratelimit.policy import PolicyName
from tests.conftest import make_settings

pytestmark = [pytest.mark.integration]


@pytest.fixture
def rl_settings() -> Settings:
    """Small, fast-refilling bucket so tests stay quick and deterministic."""
    return make_settings(
        rate_limit_enabled=True,
        rate_limit_requests=10,
        rate_limit_window_seconds=10,  # 1 token/sec
        rate_limit_burst=10,
        rate_limit_write_requests=5,
        rate_limit_write_window_seconds=10,
        rate_limit_write_burst=5,
    )


@pytest.fixture
async def rl(redis: RedisGateway, rl_settings: Settings) -> RateLimiter:
    return RateLimiter(redis, rl_settings)


class TestBucketMechanics:
    async def test_allows_up_to_burst_then_rejects(self, rl: RateLimiter) -> None:
        decisions = [await rl.check("1.2.3.4", PolicyName.READ) for _ in range(12)]
        allowed = [d for d in decisions if d.allowed]
        rejected = [d for d in decisions if not d.allowed]

        assert len(allowed) == 10, "burst capacity should admit exactly 10"
        assert len(rejected) == 2
        # Allowed decisions must count down monotonically to zero.
        assert [d.remaining for d in allowed] == list(range(9, -1, -1))

    async def test_rejection_carries_a_usable_retry_after(self, rl: RateLimiter) -> None:
        for _ in range(10):
            await rl.check("5.6.7.8", PolicyName.READ)
        denied = await rl.check("5.6.7.8", PolicyName.READ)

        assert not denied.allowed
        headers = denied.headers()
        # Retry-After must never be 0: a client honouring it would hot-loop.
        assert int(headers["Retry-After"]) >= 1
        assert headers["X-RateLimit-Remaining"] == "0"
        assert headers["X-RateLimit-Limit"] == "10"
        assert "token_bucket" in headers["X-RateLimit-Policy"]

    async def test_refills_over_time(self, rl: RateLimiter) -> None:
        """1 token/sec refill: after ~1.2s exactly one more request gets in."""
        for _ in range(10):
            await rl.check("9.9.9.9", PolicyName.READ)
        assert not (await rl.check("9.9.9.9", PolicyName.READ)).allowed

        await asyncio.sleep(1.2)

        assert (await rl.check("9.9.9.9", PolicyName.READ)).allowed
        assert not (await rl.check("9.9.9.9", PolicyName.READ)).allowed

    async def test_buckets_are_isolated_per_client(self, rl: RateLimiter) -> None:
        for _ in range(10):
            await rl.check("10.0.0.1", PolicyName.READ)
        assert not (await rl.check("10.0.0.1", PolicyName.READ)).allowed

        # A different client is unaffected.
        assert (await rl.check("10.0.0.2", PolicyName.READ)).allowed

    async def test_policies_have_independent_budgets(self, rl: RateLimiter) -> None:
        """Exhausting reads must not lock a client out of writes.

        They are separate buckets by design: a page that fires many reads
        should not consume the (much scarcer) write budget.
        """
        for _ in range(10):
            await rl.check("11.0.0.1", PolicyName.READ)
        assert not (await rl.check("11.0.0.1", PolicyName.READ)).allowed
        assert (await rl.check("11.0.0.1", PolicyName.WRITE)).allowed

    async def test_write_policy_is_stricter_than_read(self, rl: RateLimiter) -> None:
        write_allowed = 0
        for _ in range(8):
            if (await rl.check("12.0.0.1", PolicyName.WRITE)).allowed:
                write_allowed += 1
        assert write_allowed == 5

    async def test_cost_greater_than_one_consumes_proportionally(self, rl: RateLimiter) -> None:
        first = await rl.check("13.0.0.1", PolicyName.READ, cost=4)
        assert first.allowed
        assert first.remaining == 6

    async def test_request_costing_more_than_burst_is_rejected(self, rl: RateLimiter) -> None:
        decision = await rl.check("14.0.0.1", PolicyName.READ, cost=99)
        assert not decision.allowed

    async def test_disabled_limiter_always_allows(self, redis: RedisGateway) -> None:
        limiter = RateLimiter(redis, make_settings(rate_limit_enabled=False))
        for _ in range(50):
            assert (await limiter.check("15.0.0.1", PolicyName.WRITE)).allowed


class TestPrivacy:
    async def test_redis_keys_never_contain_the_raw_client_ip(
        self, rl: RateLimiter, redis: RedisGateway
    ) -> None:
        """Raw IPs are personal data; they must not be sitting in Redis keys."""
        await rl.check("203.0.113.77", PolicyName.READ)
        keys = [key async for key in redis.client.scan_iter(match="rl:*")]

        assert keys, "expected a rate-limit key to exist"
        assert all("203.0.113.77" not in key for key in keys)

    async def test_bucket_header_locates_the_key_for_an_operator(
        self, rl: RateLimiter, redis: RedisGateway
    ) -> None:
        """The hashed bucket id must be enough to find the key.

        This is what makes docs/TESTING.md's "prove the limit is shared"
        step possible without logging anyone's IP.
        """
        decision = await rl.check("203.0.113.78", PolicyName.READ)
        bucket = decision.headers()["X-RateLimit-Bucket"]

        expected_key = rl.key(rl.policy(PolicyName.READ), bucket)
        assert await redis.client.exists(expected_key) == 1

    async def test_bucket_id_is_stable_and_non_reversible(self, rl: RateLimiter) -> None:
        first = rl.bucket_id("198.51.100.9")
        assert first == rl.bucket_id("198.51.100.9")
        assert "198.51.100.9" not in first
        assert len(first) == 16


class TestDistributedBehaviour:
    async def test_two_limiter_instances_share_one_budget(
        self, redis: RedisGateway, rl_settings: Settings
    ) -> None:
        """The core horizontal-scaling claim.

        Two RateLimiter objects stand in for two API replicas: separate
        processes, separate connection pools, no shared memory. If the budget
        lived in process memory each would admit 10 and the client would get
        20 -- double the configured limit.
        """
        replica_a = RateLimiter(redis, rl_settings)
        replica_b = RateLimiter(redis, rl_settings)

        allowed = 0
        for index in range(20):
            replica = replica_a if index % 2 == 0 else replica_b
            if (await replica.check("172.20.0.5", PolicyName.READ)).allowed:
                allowed += 1

        assert allowed == 10, f"shared budget breached: {allowed} admitted, expected 10"

    async def test_separate_redis_connections_agree_on_remaining(
        self, redis: RedisGateway, rl_settings: Settings
    ) -> None:
        replica_a = RateLimiter(redis, rl_settings)
        replica_b = RateLimiter(redis, rl_settings)

        first = await replica_a.check("172.20.0.6", PolicyName.READ)
        second = await replica_b.check("172.20.0.6", PolicyName.READ)

        assert first.remaining == 9
        assert second.remaining == 8, "second replica did not observe the first's consumption"

    @pytest.mark.concurrency
    async def test_concurrent_requests_cannot_over_admit(
        self, redis: RedisGateway, rl_settings: Settings
    ) -> None:
        """The atomicity claim, under genuine concurrency.

        50 requests fired at once against a burst of 10. A naive
        GET-then-compare-then-SET limiter over-admits here, because many
        callers read the same token count before any of them writes back.
        The Lua script makes read-modify-write a single atomic step.
        """
        limiters = [RateLimiter(redis, rl_settings) for _ in range(5)]

        results = await asyncio.gather(
            *(
                limiters[index % len(limiters)].check("172.20.0.7", PolicyName.READ)
                for index in range(50)
            )
        )
        allowed = sum(1 for decision in results if decision.allowed)

        assert allowed == 10, f"over-admitted under concurrency: {allowed} of 50"

    @pytest.mark.concurrency
    async def test_concurrent_distinct_clients_are_unaffected(
        self, redis: RedisGateway, rl_settings: Settings
    ) -> None:
        """Concurrency must not leak tokens between buckets."""
        limiter = RateLimiter(redis, rl_settings)

        results = await asyncio.gather(
            *(limiter.check(f"10.10.{index}.1", PolicyName.READ) for index in range(30))
        )
        assert all(decision.allowed for decision in results)


class TestKeyHygiene:
    async def test_key_carries_a_schema_version(self, rl: RateLimiter) -> None:
        """Lets the bucket format change without colliding with old data."""
        key = rl.key(rl.policy(PolicyName.READ), "abc123")
        assert key.startswith(f"rl:v{RateLimiter.KEY_SCHEMA_VERSION}:")

    async def test_bucket_keys_expire(self, rl: RateLimiter, redis: RedisGateway) -> None:
        """Without a TTL, every IP that ever hit the API leaks a key forever."""
        decision = await rl.check("192.0.2.50", PolicyName.READ)
        key = rl.key(rl.policy(PolicyName.READ), decision.bucket)

        ttl = await redis.client.ttl(key)
        assert ttl > 0, "rate-limit key has no expiry"

    async def test_keys_are_namespaced_per_policy(self, rl: RateLimiter) -> None:
        read_key = rl.key(rl.policy(PolicyName.READ), "same-bucket")
        write_key = rl.key(rl.policy(PolicyName.WRITE), "same-bucket")
        assert read_key != write_key
