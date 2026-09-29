"""Cache-aside layer for short-code -> destination lookups.

Key format
    url:v{schema}:{short_code}
  The `v{schema}` segment means a change to the cached payload shape is a
  config bump (`CACHE_SCHEMA_VERSION`) rather than a flush-and-pray deploy:
  old and new keys coexist and the old ones age out.

Payload
    JSON. Chosen over pickle (unsafe to deserialise across versions) and over
    msgpack (an extra dependency for a payload measured in hundreds of bytes).
    Redis stores it as a plain string; entries are small enough that
    round-tripping JSON costs far less than the PostgreSQL query it replaces.

What is NOT cached
    `click_count`. It is mutated by every redirect, so caching it would mean
    either constant invalidation or knowingly serving stale counters. The
    metadata endpoint reads counts from PostgreSQL plus the un-flushed Redis
    delta instead.

Negative caching
    Unknown codes are cached as a tombstone for `CACHE_NEGATIVE_TTL_SECONDS`.
    Without it, a bot walking random 7-character codes turns into one
    PostgreSQL query per request -- a cache-penetration DoS.

Failure behaviour
    Every method here is non-raising. A dead Redis degrades the service to
    "PostgreSQL-only" (slower, still correct); it never turns a redirect into
    a 500.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from redis.exceptions import RedisError

from app.cache.redis_client import RedisGateway
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import (
    cache_errors_total,
    cache_hits_total,
    cache_invalidations_total,
    cache_misses_total,
)

logger = get_logger(__name__)

CACHE_NAME: Final = "url"
_TOMBSTONE: Final = "__missing__"


@dataclass(frozen=True, slots=True)
class CachedUrl:
    """The subset of a URL row needed to serve a redirect."""

    id: int
    short_code: str
    original_url: str
    created_at: datetime
    expires_at: datetime | None

    def is_expired(self, *, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= (now or datetime.now(UTC))

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "short_code": self.short_code,
            "original_url": self.original_url,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CachedUrl:
        expires_raw = payload.get("expires_at")
        return cls(
            id=int(payload["id"]),
            short_code=str(payload["short_code"]),
            original_url=str(payload["original_url"]),
            created_at=datetime.fromisoformat(payload["created_at"]),
            expires_at=datetime.fromisoformat(expires_raw) if expires_raw else None,
        )


class CacheState(StrEnum):
    """Outcome of a cache read, so callers can branch without magic values."""

    HIT = "hit"
    NEGATIVE_HIT = "negative_hit"
    MISS = "miss"
    DISABLED = "disabled"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CacheLookup:
    state: CacheState
    value: CachedUrl | None = None

    @property
    def is_conclusive(self) -> bool:
        """True when the cache answered definitively and PostgreSQL can be skipped."""
        return self.state in (CacheState.HIT, CacheState.NEGATIVE_HIT)


class UrlCache:
    """Cache-aside reader/writer for URL records."""

    def __init__(self, redis: RedisGateway, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    # ------------------------------------------------------------- keys ---
    def key(self, short_code: str) -> str:
        prefix = self._settings.cache_key_prefix
        return f"{prefix}:v{self._settings.cache_schema_version}:{short_code}"

    def _ttl(self) -> int:
        """Base TTL plus per-key jitter.

        Jitter matters because URLs are frequently created in bursts (an import,
        a campaign). Without it, every key from that burst expires in the same
        second and the resulting thundering herd hits PostgreSQL all at once.
        """
        jitter = self._settings.cache_ttl_jitter_seconds
        return self._settings.cache_ttl_seconds + (
            random.randint(0, jitter) if jitter else 0  # noqa: S311 - jitter, not security
        )

    # ------------------------------------------------------------- read ---
    async def get(self, short_code: str) -> CacheLookup:
        """Read a cache entry. Never raises."""
        if not self._settings.cache_enabled:
            return CacheLookup(CacheState.DISABLED)

        try:
            async with self._redis.track("cache_get"):
                raw = await self._redis.client.get(self.key(short_code))
        except (RedisError, OSError) as exc:
            cache_errors_total.labels(cache=CACHE_NAME, operation="get").inc()
            logger.warning("cache read failed", extra={"reason": type(exc).__name__})
            return CacheLookup(CacheState.ERROR)

        if raw is None:
            cache_misses_total.labels(cache=CACHE_NAME).inc()
            return CacheLookup(CacheState.MISS)

        if raw == _TOMBSTONE:
            cache_hits_total.labels(cache=CACHE_NAME, kind="negative").inc()
            return CacheLookup(CacheState.NEGATIVE_HIT)

        try:
            value = CachedUrl.from_payload(json.loads(raw))
        except (ValueError, KeyError, TypeError) as exc:
            # A malformed entry means a bad/older writer. Treat as a miss and
            # let the caller repopulate rather than failing the request.
            cache_errors_total.labels(cache=CACHE_NAME, operation="decode").inc()
            logger.warning("cache entry decode failed", extra={"reason": type(exc).__name__})
            cache_misses_total.labels(cache=CACHE_NAME).inc()
            return CacheLookup(CacheState.MISS)

        cache_hits_total.labels(cache=CACHE_NAME, kind="value").inc()
        return CacheLookup(CacheState.HIT, value)

    # ------------------------------------------------------------ write ---
    async def set(self, value: CachedUrl) -> None:
        """Populate a cache entry. Never raises.

        TTL is clamped to the URL's own expiry so an expired link cannot be
        served from cache after PostgreSQL considers it gone.
        """
        if not self._settings.cache_enabled:
            return

        ttl = self._ttl()
        if value.expires_at is not None:
            remaining = int((value.expires_at - datetime.now(UTC)).total_seconds())
            if remaining <= 0:
                return
            ttl = min(ttl, remaining)

        try:
            async with self._redis.track("cache_set"):
                await self._redis.client.set(
                    self.key(value.short_code),
                    json.dumps(value.to_payload(), separators=(",", ":")),
                    ex=ttl,
                )
        except (RedisError, OSError) as exc:
            cache_errors_total.labels(cache=CACHE_NAME, operation="set").inc()
            logger.warning("cache write failed", extra={"reason": type(exc).__name__})

    async def set_missing(self, short_code: str) -> None:
        """Write a negative-cache tombstone. Never raises."""
        if not self._settings.cache_enabled or self._settings.cache_negative_ttl_seconds <= 0:
            return
        try:
            async with self._redis.track("cache_set_missing"):
                await self._redis.client.set(
                    self.key(short_code),
                    _TOMBSTONE,
                    ex=self._settings.cache_negative_ttl_seconds,
                )
        except (RedisError, OSError) as exc:
            cache_errors_total.labels(cache=CACHE_NAME, operation="set_missing").inc()
            logger.warning("negative cache write failed", extra={"reason": type(exc).__name__})

    async def invalidate(self, short_code: str, *, reason: str) -> None:
        """Drop a cache entry. Never raises.

        Called on delete and on any mutation. Deliberately a DEL and not a
        rewrite: the next reader repopulates from PostgreSQL, so there is no
        window where the cache holds a value the database never had.
        """
        if not self._settings.cache_enabled:
            return
        try:
            async with self._redis.track("cache_invalidate"):
                await self._redis.client.delete(self.key(short_code))
        except (RedisError, OSError) as exc:
            cache_errors_total.labels(cache=CACHE_NAME, operation="invalidate").inc()
            # Escalated to ERROR: a failed invalidation is the one cache failure
            # that can serve wrong data (up to one TTL of a deleted link).
            logger.error(
                "cache invalidation failed; entry will serve stale until TTL",
                extra={
                    "reason": type(exc).__name__,
                    "ttl_seconds": self._settings.cache_ttl_seconds,
                },
            )
        else:
            cache_invalidations_total.labels(cache=CACHE_NAME, reason=reason).inc()
