"""Cache-aside behaviour against a real Redis.

Asserts the properties that make the cache safe rather than merely fast:
it must never serve a deleted link, never let one client's cached entry
outlive the row it mirrors, and never turn a Redis outage into an API outage.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.cache.redis_client import RedisGateway
from app.cache.url_cache import CachedUrl, CacheState, UrlCache
from app.core.config import Settings
from tests.conftest import make_settings

pytestmark = [pytest.mark.integration]


def sample(code: str = "abc1234", url: str = "https://example.com/x") -> CachedUrl:
    now = datetime.now(UTC)
    return CachedUrl(
        id=1,
        short_code=code,
        original_url=url,
        created_at=now,
        expires_at=None,
    )


class TestCacheAside:
    async def test_miss_then_hit(self, cache: UrlCache) -> None:
        assert (await cache.get("abc1234")).state is CacheState.MISS

        await cache.set(sample())
        lookup = await cache.get("abc1234")

        assert lookup.state is CacheState.HIT
        assert lookup.value is not None
        assert lookup.value.original_url == "https://example.com/x"

    async def test_round_trip_preserves_every_field(self, cache: UrlCache) -> None:
        expires = datetime.now(UTC) + timedelta(days=1)
        original = CachedUrl(
            id=42,
            short_code="rt12345",
            original_url="https://example.com/round/trip?a=1#frag",
            created_at=datetime.now(UTC),
            expires_at=expires,
        )
        await cache.set(original)
        restored = (await cache.get("rt12345")).value

        assert restored is not None
        assert restored.id == original.id
        assert restored.short_code == original.short_code
        assert restored.original_url == original.original_url
        # Serialisation must survive a timezone-aware datetime intact.
        assert restored.expires_at is not None
        assert abs((restored.expires_at - expires).total_seconds()) < 1

    async def test_invalidate_removes_the_entry(self, cache: UrlCache) -> None:
        await cache.set(sample())
        await cache.invalidate("abc1234", reason="deleted")

        assert (await cache.get("abc1234")).state is CacheState.MISS

    async def test_entries_have_a_bounded_ttl(
        self, cache: UrlCache, redis: RedisGateway, settings: Settings
    ) -> None:
        """An entry without a TTL is a permanent stale-data risk."""
        await cache.set(sample())
        ttl = await redis.client.ttl(cache.key("abc1234"))

        assert 0 < ttl <= settings.cache_ttl_seconds + settings.cache_ttl_jitter_seconds

    async def test_ttl_is_jittered_across_keys(self, cache: UrlCache, redis: RedisGateway) -> None:
        """Identical TTLs make a bulk import expire in one thundering herd.

        Jitter spreads the expiry of keys written together, so the database
        sees a trickle of refills rather than a spike.
        """
        for index in range(30):
            await cache.set(sample(code=f"jit{index:04d}"))

        ttls = {await redis.client.ttl(cache.key(f"jit{index:04d}")) for index in range(30)}
        assert len(ttls) > 1, "all keys share one TTL; jitter is not applied"

    async def test_cache_key_is_namespaced_and_versioned(
        self, cache: UrlCache, settings: Settings
    ) -> None:
        """A schema version lets the payload format change without a flush."""
        key = cache.key("abc1234")
        assert key.startswith(f"{settings.cache_key_prefix}:v{settings.cache_schema_version}:")
        assert key.endswith("abc1234")

    async def test_disabled_cache_reports_disabled_not_miss(self, redis: RedisGateway) -> None:
        """The baseline benchmark relies on this distinction.

        `disabled` must not be counted as a miss, or the baseline run would
        report a 0% hit ratio *and* inflate the miss counter, making the two
        benchmark arms incomparable.
        """
        disabled = UrlCache(redis, make_settings(cache_enabled=False))
        await disabled.set(sample())

        assert (await disabled.get("abc1234")).state is CacheState.DISABLED

    async def test_disabled_cache_writes_nothing_to_redis(self, redis: RedisGateway) -> None:
        disabled = UrlCache(redis, make_settings(cache_enabled=False))
        await disabled.set(sample())

        assert [key async for key in redis.client.scan_iter(match="url:*")] == []


class TestNegativeCaching:
    async def test_missing_code_is_remembered(self, cache: UrlCache) -> None:
        """Cache penetration guard.

        Without this, a bot walking random codes turns every request into a
        PostgreSQL query -- a trivially cheap way to load the database.
        """
        await cache.set_missing("nosuch1")
        lookup = await cache.get("nosuch1")

        assert lookup.state is CacheState.NEGATIVE_HIT
        assert lookup.value is None

    async def test_negative_entry_expires_sooner_than_a_value(
        self, cache: UrlCache, redis: RedisGateway, settings: Settings
    ) -> None:
        """A tombstone must be short-lived.

        It is the window during which a newly created code would still 404,
        so it trades penetration protection against staleness.
        """
        await cache.set_missing("nosuch2")
        ttl = await redis.client.ttl(cache.key("nosuch2"))

        assert 0 < ttl <= settings.cache_negative_ttl_seconds
        assert settings.cache_negative_ttl_seconds < settings.cache_ttl_seconds

    async def test_a_real_value_overwrites_a_tombstone(self, cache: UrlCache) -> None:
        await cache.set_missing("abc1234")
        await cache.set(sample())

        lookup = await cache.get("abc1234")
        assert lookup.state is CacheState.HIT

    async def test_negative_caching_disabled_by_zero_ttl(self, redis: RedisGateway) -> None:
        cache = UrlCache(redis, make_settings(cache_negative_ttl_seconds=0))
        await cache.set_missing("nosuch3")

        assert (await cache.get("nosuch3")).state is CacheState.MISS


class TestExpiryInteraction:
    async def test_ttl_never_outlives_the_link(self, cache: UrlCache, redis: RedisGateway) -> None:
        """A cached entry outliving its link would serve a dead redirect.

        The link expires in 5s, so the cache TTL must be clamped to <= 5s even
        though the configured default is minutes.
        """
        expires = datetime.now(UTC) + timedelta(seconds=5)
        await cache.set(
            CachedUrl(
                id=1,
                short_code="exp1234",
                original_url="https://example.com/x",
                created_at=datetime.now(UTC),
                expires_at=expires,
            )
        )
        ttl = await redis.client.ttl(cache.key("exp1234"))

        assert 0 < ttl <= 6

    async def test_already_expired_link_is_not_cached(self, cache: UrlCache) -> None:
        await cache.set(
            CachedUrl(
                id=1,
                short_code="exp5678",
                original_url="https://example.com/x",
                created_at=datetime.now(UTC) - timedelta(hours=2),
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        assert (await cache.get("exp5678")).state is not CacheState.HIT


class TestCorruptData:
    async def test_undecodable_payload_degrades_to_a_miss(
        self, cache: UrlCache, redis: RedisGateway
    ) -> None:
        """A bad payload must not 500 the redirect path.

        This happens for real during a rolling deploy that changes the payload
        shape, or if anything else writes to our keyspace. Falling back to
        PostgreSQL is always safe.
        """
        await redis.client.set(cache.key("bad1234"), "{not valid json")
        lookup = await cache.get("bad1234")

        assert lookup.state in {CacheState.MISS, CacheState.ERROR}
        assert lookup.value is None

    async def test_json_of_the_wrong_shape_degrades_to_a_miss(
        self, cache: UrlCache, redis: RedisGateway
    ) -> None:
        await redis.client.set(cache.key("bad5678"), '{"unexpected": true}')
        lookup = await cache.get("bad5678")

        assert lookup.state in {CacheState.MISS, CacheState.ERROR}


class TestSharedAcrossInstances:
    async def test_one_instance_reads_what_another_wrote(
        self, redis: RedisGateway, settings: Settings
    ) -> None:
        """Two UrlCache objects stand in for two API replicas.

        If the cache were process-local, replica B would miss and each replica
        would maintain its own copy -- halving the hit rate and doubling the
        window for stale reads.
        """
        replica_a = UrlCache(redis, settings)
        replica_b = UrlCache(redis, settings)

        await replica_a.set(sample(code="shared1"))
        lookup = await replica_b.get("shared1")

        assert lookup.state is CacheState.HIT

    async def test_invalidation_by_one_instance_is_seen_by_the_other(
        self, redis: RedisGateway, settings: Settings
    ) -> None:
        """The correctness half of a shared cache.

        A delete handled by replica A must immediately stop replica B from
        serving the link, or the link stays alive on whichever replicas happen
        to hold it.
        """
        replica_a = UrlCache(redis, settings)
        replica_b = UrlCache(redis, settings)

        await replica_a.set(sample(code="shared2"))
        assert (await replica_b.get("shared2")).state is CacheState.HIT

        await replica_a.invalidate("shared2", reason="deleted")
        assert (await replica_b.get("shared2")).state is CacheState.MISS

    @pytest.mark.concurrency
    async def test_concurrent_writes_of_the_same_key_converge(
        self, redis: RedisGateway, settings: Settings
    ) -> None:
        """Cache stampede: many replicas refill the same key at once.

        Every writer stores an identical value, so last-write-wins is
        harmless -- but it must not corrupt the entry or leave it absent.
        """
        caches = [UrlCache(redis, settings) for _ in range(10)]
        await asyncio.gather(*(cache.set(sample(code="stamped")) for cache in caches))

        lookup = await caches[0].get("stamped")
        assert lookup.state is CacheState.HIT
        assert lookup.value is not None
        assert lookup.value.original_url == "https://example.com/x"
