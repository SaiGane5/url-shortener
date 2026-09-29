"""Shared test fixtures.

Two tiers of test run from this file:

* **Unit tests** (`tests/unit`) touch no network. They exercise pure logic --
  code generation, URL validation, schema validation, error mapping -- and run
  anywhere, including CI without services.
* **Integration tests** (`tests/integration`) run against a *real* PostgreSQL
  and a *real* Redis. Fakes were deliberately not used: the two things most
  worth testing here are a Lua token bucket executing atomically inside Redis
  and PostgreSQL enforcing a unique constraint under concurrent inserts.
  Neither behaviour survives being mocked -- a fake would only assert that our
  mock behaves the way we already assumed it does.

Integration tests get their own database (`urlshort_test`), created once per
session, so a test run can never truncate data the developer is looking at in
the dev stack.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.cache.redis_client import RedisGateway
from app.cache.url_cache import UrlCache
from app.core.config import Settings
from app.db.base import Base
from app.db.session import Database
from app.ratelimit.limiter import RateLimiter
from app.repositories.url_repository import UrlRepository
from app.services.click_tracker import ClickTracker
from app.services.url_service import UrlService

# Host-side addresses of the docker-compose dependencies. Compose maps
# PostgreSQL to 5433 and Redis to 6380 to avoid clashing with anything the
# developer already runs locally, so tests default to those and can be
# overridden when running inside the compose network (CI does this).
DEFAULT_TEST_DB_URL = "postgresql+asyncpg://urlshort:urlshort@localhost:5433/urlshort_test"
DEFAULT_TEST_REDIS_URL = "redis://localhost:6380/9"  # db 9: never used by the app

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DB_URL)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", DEFAULT_TEST_REDIS_URL)


def _admin_url(database_url: str) -> str:
    """Same server, but connected to `postgres` so we can CREATE DATABASE."""
    base, _, _ = database_url.rpartition("/")
    return f"{base}/postgres"


def _database_name(database_url: str) -> str:
    return database_url.rpartition("/")[2]


def make_settings(**overrides: Any) -> Settings:
    """Build Settings for tests, ignoring the developer's .env entirely.

    `_env_file=None` matters: without it a local .env would leak into tests and
    make results depend on the machine.
    """
    defaults: dict[str, Any] = {
        "app_env": "test",
        "log_level": "WARNING",
        "log_format": "console",
        "instance_id": "test-instance",
        "public_base_url": "http://testserver",
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "cache_enabled": True,
        "rate_limit_enabled": False,  # opted into explicitly per test
        "click_flush_enabled": True,
        # Tests assert on validation of private targets in both directions, so
        # keep the production-safe default and override where needed.
        "allow_private_url_targets": False,
        "docs_enabled": True,
        "metrics_enabled": True,
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return make_settings()


# --------------------------------------------------------------- database ---


# One-time-per-session guards. These are deliberately module flags rather than
# session-scoped async fixtures: pytest-asyncio gives each test its own event
# loop, and a session-scoped async fixture would bind asyncpg connections to a
# loop that later tests no longer run on -- the classic "attached to a
# different loop" flake. Each call below builds and disposes its own engine on
# whatever loop is current, and the flag keeps the expensive DDL to one run.
_database_created = False
_schema_created = False


@pytest_asyncio.fixture
async def _create_test_database() -> AsyncIterator[None]:
    """Create the dedicated test database once per session if absent."""
    global _database_created
    if _database_created:
        yield
        return

    engine = create_async_engine(
        _admin_url(TEST_DATABASE_URL),
        isolation_level="AUTOCOMMIT",  # CREATE DATABASE cannot run in a transaction
        poolclass=NullPool,
    )
    name = _database_name(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            exists = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": name},
            )
            if exists.first() is None:
                # Identifier, not a value, so it cannot be bound. The name comes
                # from our own constant, never from user input.
                await conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    finally:
        await engine.dispose()

    _database_created = True
    yield


@pytest_asyncio.fixture
async def _schema(_create_test_database: None) -> AsyncIterator[None]:
    """Create the schema from the ORM metadata.

    Deliberately *not* Alembic: a test run should not depend on migration
    history being replayable. `tests/integration/test_migrations.py` covers
    the migration path separately, including that it matches this metadata.
    """
    global _schema_created
    if _schema_created:
        yield
        return

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()

    _schema_created = True
    yield


@pytest_asyncio.fixture
async def database(_schema: None, settings: Settings) -> AsyncIterator[Database]:
    db = Database(settings)
    yield db
    await db.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Truncate between integration tests so each starts from empty.

    Autouse but cheap: unit tests do not request `database`, and this fixture
    no-ops unless the test actually pulled in the schema fixture.
    """
    yield
    if "_schema" not in request.fixturenames:
        return
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("TRUNCATE TABLE urls RESTART IDENTITY CASCADE")
    finally:
        await engine.dispose()


# ------------------------------------------------------------------ redis ---


@pytest_asyncio.fixture
async def redis(settings: Settings) -> AsyncIterator[RedisGateway]:
    """A real Redis on db 9, flushed before and after each test."""
    gateway = RedisGateway(settings)
    await gateway.client.flushdb()
    yield gateway
    await gateway.client.flushdb()
    await gateway.close()


# --------------------------------------------------------------- services ---


@pytest.fixture
def cache(redis: RedisGateway, settings: Settings) -> UrlCache:
    return UrlCache(redis, settings)


@pytest.fixture
def click_tracker(redis: RedisGateway, database: Database, settings: Settings) -> ClickTracker:
    return ClickTracker(redis, database, settings)


@pytest.fixture
def limiter(redis: RedisGateway, settings: Settings) -> RateLimiter:
    return RateLimiter(redis, settings)


@pytest.fixture
def url_service(
    database: Database,
    cache: UrlCache,
    click_tracker: ClickTracker,
    settings: Settings,
) -> UrlService:
    return UrlService(
        db=database,
        cache=cache,
        clicks=click_tracker,
        settings=settings,
    )


@pytest.fixture
def repository_factory(database: Database) -> Any:
    """Callable yielding a repository bound to a fresh session."""
    return lambda session: UrlRepository(session)


# ----------------------------------------------------------- app + client ---


@pytest_asyncio.fixture
async def app_factory(_schema: None) -> AsyncIterator[Any]:
    """Build a fully wired app with per-test settings overrides.

    Uses the real lifespan so startup/shutdown behaviour (dependency pings,
    click-flusher start/stop) is covered rather than bypassed.
    """
    from app.main import create_app

    created: list[Any] = []

    async def _best_effort_flush(gateway: RedisGateway) -> None:
        """Reset Redis between apps, tolerating a deliberately dead Redis.

        The failure-mode tests build apps pointed at an unreachable Redis on
        purpose, so a hard flush here would fail the fixture instead of the
        behaviour under test.
        """
        with contextlib.suppress(OSError, RedisError):
            await gateway.client.flushdb()

    async def _factory(**overrides: Any) -> AsyncClient:
        app = create_app(make_settings(**overrides))
        manager = app.router.lifespan_context(app)
        await manager.__aenter__()
        await _best_effort_flush(app.state.redis)
        transport = ASGITransport(app=app)
        client = AsyncClient(
            transport=transport,
            base_url="http://testserver",
            follow_redirects=False,
        )
        created.append((app, manager, client))
        return client

    yield _factory

    for app, manager, client in reversed(created):
        await client.aclose()
        await _best_effort_flush(app.state.redis)
        await manager.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def client(app_factory: Any) -> AsyncClient:
    """Default app: caching on, rate limiting off."""
    built: AsyncClient = await app_factory()
    return built
