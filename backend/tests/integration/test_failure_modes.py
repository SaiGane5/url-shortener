"""Dependency-failure behaviour.

Outages are simulated by pointing a component at a closed port, which produces
a genuine connection error from the real client library -- including its real
timeout behaviour -- rather than a mock's idea of one. The live compose stack
is left untouched, so these run safely alongside it.

The behaviour under test is the asymmetry documented in
docs/ENGINEERING_DECISIONS.md:

* Redis down, reads/redirects  -> FAIL OPEN. A cache outage must not become a
  site outage; serving from PostgreSQL unmetered is better than serving 503.
* Redis down, writes           -> FAIL CLOSED. An unmetered write path is the
  one thing worth refusing, because it is the abusable, expensive direction.
* PostgreSQL down              -> 503 everywhere. It is the source of truth;
  there is nothing honest to serve without it.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.cache.redis_client import RedisGateway
from app.cache.url_cache import CacheState, UrlCache
from app.core.config import Settings
from app.ratelimit.limiter import RateLimiter
from app.ratelimit.policy import PolicyName
from tests.conftest import make_settings

pytestmark = [pytest.mark.integration, pytest.mark.failure]

#: Reserved discard port: nothing listens, so connections are refused fast.
DEAD_REDIS_URL = "redis://127.0.0.1:9/0"
DEAD_POSTGRES_URL = "postgresql+asyncpg://nobody:nobody@127.0.0.1:9/nothing"


def offline_settings(**overrides: Any) -> Settings:
    """Settings whose Redis is unreachable, with fast timeouts."""
    return make_settings(
        redis_url=DEAD_REDIS_URL,
        redis_socket_timeout_seconds=0.05,
        redis_connect_timeout_seconds=0.05,
        **overrides,
    )


class TestRedisUnavailableCache:
    async def test_cache_get_reports_error_and_does_not_raise(self) -> None:
        """The redirect path must survive this without an exception.

        Anything that escapes here becomes a 500 on every redirect during a
        Redis outage, which is exactly the failure the cache was supposed to
        protect against.
        """
        gateway = RedisGateway(offline_settings())
        try:
            lookup = await UrlCache(gateway, offline_settings()).get("abc1234")
            assert lookup.state is CacheState.ERROR
            assert lookup.value is None
        finally:
            await gateway.close()

    async def test_cache_set_swallows_the_failure(self) -> None:
        """Failing to populate the cache is not a request-failing event."""
        from tests.integration.test_cache import sample

        gateway = RedisGateway(offline_settings())
        try:
            await UrlCache(gateway, offline_settings()).set(sample())
        finally:
            await gateway.close()

    async def test_cache_invalidate_swallows_the_failure(self) -> None:
        """A delete must still succeed when Redis is down.

        The row is gone from PostgreSQL, which is the source of truth; the
        stale cache entry expires on its own TTL.
        """
        gateway = RedisGateway(offline_settings())
        try:
            await UrlCache(gateway, offline_settings()).invalidate("abc1234", reason="deleted")
        finally:
            await gateway.close()


class TestRedisUnavailableRateLimiter:
    async def test_read_policy_fails_open(self) -> None:
        settings = offline_settings(rate_limit_enabled=True, rate_limit_fail_open=True)
        gateway = RedisGateway(settings)
        try:
            decision = await RateLimiter(gateway, settings).check("1.2.3.4", PolicyName.READ)

            assert decision.allowed, "read policy must fail open"
            assert decision.degraded
            assert decision.headers()["X-RateLimit-Degraded"] == "true"
        finally:
            await gateway.close()

    async def test_redirect_policy_fails_open(self) -> None:
        settings = offline_settings(rate_limit_enabled=True, rate_limit_fail_open=True)
        gateway = RedisGateway(settings)
        try:
            decision = await RateLimiter(gateway, settings).check("1.2.3.4", PolicyName.REDIRECT)
            assert decision.allowed
        finally:
            await gateway.close()

    async def test_write_policy_fails_closed_by_default(self) -> None:
        settings = offline_settings(
            rate_limit_enabled=True,
            rate_limit_write_fail_open=False,
        )
        gateway = RedisGateway(settings)
        try:
            decision = await RateLimiter(gateway, settings).check("1.2.3.4", PolicyName.WRITE)

            assert not decision.allowed, "write policy must fail closed"
            assert decision.degraded
            assert int(decision.headers()["Retry-After"]) >= 1
        finally:
            await gateway.close()

    async def test_write_fail_open_is_configurable(self) -> None:
        """The toggle exists so a demo can keep accepting writes.

        Documented as unsafe for production in docs/ENGINEERING_DECISIONS.md.
        """
        settings = offline_settings(rate_limit_enabled=True, rate_limit_write_fail_open=True)
        gateway = RedisGateway(settings)
        try:
            decision = await RateLimiter(gateway, settings).check("1.2.3.4", PolicyName.WRITE)
            assert decision.allowed
        finally:
            await gateway.close()

    async def test_limiter_never_raises_on_outage(self) -> None:
        """`check()` promises never to raise; the middleware depends on it."""
        settings = offline_settings(rate_limit_enabled=True)
        gateway = RedisGateway(settings)
        try:
            limiter = RateLimiter(gateway, settings)
            for policy in (PolicyName.READ, PolicyName.WRITE, PolicyName.REDIRECT):
                await limiter.check("1.2.3.4", policy)
        finally:
            await gateway.close()


class TestRedisUnavailableEndToEnd:
    async def test_redirects_still_work_from_postgresql(self, app_factory: Any) -> None:
        """The headline resilience claim, exercised over HTTP.

        The link is created while Redis is healthy, then the app is rebuilt
        pointing at a dead Redis. Redirects must keep working by reading
        PostgreSQL directly.
        """
        healthy = await app_factory()
        created = await healthy.post("/api/v1/urls", json={"url": "https://example.com/survives"})
        code = created.json()["short_code"]

        degraded = await app_factory(
            redis_url=DEAD_REDIS_URL,
            redis_socket_timeout_seconds=0.05,
            redis_connect_timeout_seconds=0.05,
            rate_limit_enabled=True,
            rate_limit_fail_open=True,
        )
        response = await degraded.get(f"/{code}")

        assert response.status_code == 307
        assert response.headers["location"] == "https://example.com/survives"

    async def test_metadata_still_works(self, app_factory: Any) -> None:
        healthy = await app_factory()
        created = await healthy.post("/api/v1/urls", json={"url": "https://example.com/meta"})
        code = created.json()["short_code"]

        degraded = await app_factory(
            redis_url=DEAD_REDIS_URL,
            redis_socket_timeout_seconds=0.05,
            redis_connect_timeout_seconds=0.05,
        )
        response = await degraded.get(f"/api/v1/urls/{code}")

        assert response.status_code == 200
        assert response.json()["short_code"] == code

    async def test_writes_are_refused_when_the_limiter_cannot_meter_them(
        self, app_factory: Any
    ) -> None:
        client = await app_factory(
            redis_url=DEAD_REDIS_URL,
            redis_socket_timeout_seconds=0.05,
            redis_connect_timeout_seconds=0.05,
            rate_limit_enabled=True,
            rate_limit_write_fail_open=False,
        )
        response = await client.post("/api/v1/urls", json={"url": "https://example.com/x"})

        assert response.status_code in {429, 503}
        assert response.json()["error"]["code"]

    async def test_readiness_reports_degraded_but_stays_serving(self, app_factory: Any) -> None:
        """Redis down must not pull the replica out of the load balancer.

        Every replica shares one Redis, so returning 503 would remove *all*
        of them and turn a degradation into a total outage.
        """
        client = await app_factory(
            redis_url=DEAD_REDIS_URL,
            redis_socket_timeout_seconds=0.05,
            redis_connect_timeout_seconds=0.05,
        )
        response = await client.get("/ready")
        body = response.json()

        assert response.status_code == 200
        assert body["degraded"] is True
        assert body["dependencies"]["redis"] is False
        assert body["dependencies"]["postgres"] is True

    async def test_liveness_is_unaffected_by_dependencies(self, app_factory: Any) -> None:
        """`/health` must not probe dependencies.

        If it did, a Redis blip would make Docker/Kubernetes kill and restart
        healthy containers -- turning a degradation into a crash loop.
        """
        client = await app_factory(
            redis_url=DEAD_REDIS_URL,
            redis_socket_timeout_seconds=0.05,
            redis_connect_timeout_seconds=0.05,
        )
        response = await client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestPostgresUnavailable:
    async def test_readiness_fails_when_the_database_is_down(self, app_factory: Any) -> None:
        """503 here is correct: the replica genuinely cannot serve.

        Unlike Redis, PostgreSQL is per-replica reachable and is the source of
        truth, so removing this replica from the pool may actually help.
        """
        client = await app_factory(
            database_url=DEAD_POSTGRES_URL,
            db_pool_timeout_seconds=0.5,
            db_command_timeout_seconds=0.5,
        )
        response = await client.get("/ready")

        assert response.status_code == 503
        assert response.json()["dependencies"]["postgres"] is False

    async def test_create_returns_503_not_500(self, app_factory: Any) -> None:
        """A dependency outage is not an application bug.

        503 tells the client to retry; 500 tells it the request will never
        work. Getting this wrong makes clients give up during a blip.
        """
        client = await app_factory(
            database_url=DEAD_POSTGRES_URL,
            db_pool_timeout_seconds=0.5,
            db_command_timeout_seconds=0.5,
        )
        response = await client.post("/api/v1/urls", json={"url": "https://example.com/x"})

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "dependency_unavailable"

    async def test_redirect_returns_503_on_cache_miss(self, app_factory: Any) -> None:
        client = await app_factory(
            database_url=DEAD_POSTGRES_URL,
            db_pool_timeout_seconds=0.5,
            db_command_timeout_seconds=0.5,
        )
        response = await client.get("/someCode")

        assert response.status_code == 503

    async def test_error_response_leaks_no_connection_details(self, app_factory: Any) -> None:
        """A driver error string can carry hostnames, ports, and usernames."""
        client = await app_factory(
            database_url=DEAD_POSTGRES_URL,
            db_pool_timeout_seconds=0.5,
            db_command_timeout_seconds=0.5,
        )
        body = (await client.post("/api/v1/urls", json={"url": "https://example.com/x"})).text

        for leak in ("nobody", "127.0.0.1", "asyncpg", "Traceback", "password"):
            assert leak not in body, f"error response leaked {leak!r}"
