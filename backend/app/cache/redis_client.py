"""Redis connection management and instrumentation.

One `RedisGateway` per process owns a connection pool shared by the cache, the
rate limiter, and the click buffer. Timeouts are deliberately aggressive
(250 ms by default): Redis sits on the hot path of every request, so a slow
Redis must degrade into "treat it as unavailable" rather than into a queue of
requests all blocked on the same socket.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import dependency_latency_seconds, dependency_up, redis_errors_total

logger = get_logger(__name__)


class RedisGateway:
    """Thin, instrumented wrapper around an async Redis client."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Redis = Redis.from_url(
            settings.redis_url,
            max_connections=settings.redis_max_connections,
            socket_timeout=settings.redis_socket_timeout_seconds,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_keepalive=True,
            retry_on_timeout=False,
            decode_responses=True,
        )

    @property
    def client(self) -> Redis:
        return self._client

    @asynccontextmanager
    async def track(self, operation: str) -> AsyncIterator[None]:
        """Record latency for a Redis operation and count failures.

        Does not swallow the exception -- callers decide whether their operation
        is safe to degrade (cache reads: yes) or must surface (nothing, today).
        """
        started = time.perf_counter()
        try:
            yield
        except (RedisError, OSError):
            redis_errors_total.labels(operation=operation).inc()
            raise
        else:
            dependency_latency_seconds.labels(dependency="redis", operation=operation).observe(
                time.perf_counter() - started
            )

    async def ping(self) -> bool:
        """Readiness probe. Never raises."""
        started = time.perf_counter()
        try:
            await self._client.ping()
        except (RedisError, OSError) as exc:
            redis_errors_total.labels(operation="ping").inc()
            dependency_up.labels(dependency="redis").set(0)
            logger.warning("redis ping failed", extra={"reason": type(exc).__name__})
            return False
        else:
            dependency_latency_seconds.labels(dependency="redis", operation="ping").observe(
                time.perf_counter() - started
            )
            dependency_up.labels(dependency="redis").set(1)
            return True

    async def close(self) -> None:
        await self._client.aclose()
        logger.info("redis connections closed")

    async def __aenter__(self) -> RedisGateway:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
