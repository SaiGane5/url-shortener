"""Async SQLAlchemy engine + session management.

The engine is created once per process and owns a connection pool. Sizing
matters here: `db_pool_size * api_replicas` must stay comfortably under
PostgreSQL's `max_connections`, which is why the pool defaults are modest and
configurable (docs/PERFORMANCE.md covers the tuning we actually measured).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import text

from app.core.config import Settings
from app.core.errors import DependencyUnavailableError
from app.core.logging import get_logger
from app.core.metrics import dependency_latency_seconds, dependency_up, postgres_errors_total

logger = get_logger(__name__)


class Database:
    """Owns the async engine and hands out sessions."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # NullPool under pytest: each test gets a fresh event loop, and pooled
        # asyncpg connections bound to a dead loop are the classic source of
        # "attached to a different loop" flakes.
        use_pool = settings.app_env != "test"
        self._engine: AsyncEngine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            future=True,
            pool_pre_ping=True,
            **(
                {
                    "pool_size": settings.db_pool_size,
                    "max_overflow": settings.db_max_overflow,
                    "pool_timeout": settings.db_pool_timeout_seconds,
                    "pool_recycle": settings.db_pool_recycle_seconds,
                }
                if use_pool
                else {"poolclass": NullPool}
            ),
            connect_args={
                "command_timeout": settings.db_command_timeout_seconds,
                "server_settings": {"application_name": f"url-shortener:{settings.instance_id}"},
            },
        )
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session; roll back on failure.

        Connection-level failures are translated to `DependencyUnavailableError`
        here rather than left to the global handler. The asyncpg dialect does
        *not* wrap a refused connection in `SQLAlchemyError` -- the raw
        `ConnectionRefusedError` (an `OSError`) propagates -- so without this
        translation a database outage surfaces as a 500 rather than a 503.
        That distinction matters to callers: 503 with `Retry-After` invites a
        retry, while 500 tells a well-behaved client the request will never
        succeed.
        """
        async with self._session_factory() as session:
            try:
                yield session
            except OSError as exc:
                await session.rollback()
                postgres_errors_total.labels(operation="connect").inc()
                dependency_up.labels(dependency="postgres").set(0)
                logger.warning(
                    "postgres connection failed",
                    extra={"reason": type(exc).__name__},
                )
                raise DependencyUnavailableError("The database is unavailable.") from exc
            except Exception:
                await session.rollback()
                raise

    async def ping(self) -> bool:
        """Readiness probe: can we round-trip a trivial query?"""
        started = time.perf_counter()
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError) as exc:
            postgres_errors_total.labels(operation="ping").inc()
            dependency_up.labels(dependency="postgres").set(0)
            logger.warning("postgres ping failed", extra={"reason": type(exc).__name__})
            return False
        else:
            dependency_latency_seconds.labels(dependency="postgres", operation="ping").observe(
                time.perf_counter() - started
            )
            dependency_up.labels(dependency="postgres").set(1)
            return True

    async def dispose(self) -> None:
        await self._engine.dispose()
        logger.info("database engine disposed")
