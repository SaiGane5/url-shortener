"""Application service for shortened URLs.

This layer owns the interesting decisions:

* the cache-aside read path for redirects,
* the generate-insert-retry loop that makes short-code collisions a non-event,
* transaction boundaries,
* invalidate-after-commit ordering on delete.

It deliberately owns its own database sessions instead of receiving one via
FastAPI's `Depends`. The reason is the redirect hot path: on a cache hit it
must do *zero* PostgreSQL work, and a request-scoped session dependency would
check a connection out of the pool for every redirect whether or not the
request ever needed it. At redirect volumes that is the difference between a
pool sized for cache misses and a pool sized for total traffic.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

from app.cache.url_cache import CachedUrl, CacheState, UrlCache
from app.core.config import Settings
from app.core.errors import (
    AliasUnavailableError,
    CustomAliasDisabledError,
    ShortCodeConflictError,
    ShortCodeExpiredError,
    ShortCodeGenerationError,
    ShortCodeNotFoundError,
)
from app.core.logging import get_logger, safe_url
from app.core.metrics import (
    clicks_recorded_total,
    short_code_collisions_total,
    short_code_generation_failures_total,
    url_redirects_total,
    urls_created_total,
    urls_deleted_total,
)
from app.db.models import Url
from app.db.session import Database
from app.repositories.url_repository import UrlRepository
from app.schemas.url import UrlStatus
from app.services.click_tracker import ClickTracker
from app.services.short_code import generate_short_code, validate_short_code
from app.services.url_validation import validate_destination_url

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UrlRecord:
    """Transport-agnostic view of a shortened URL."""

    id: int
    short_code: str
    original_url: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    click_count: int

    @classmethod
    def from_model(cls, model: Url, *, extra_clicks: int = 0) -> UrlRecord:
        return cls(
            id=model.id,
            short_code=model.short_code,
            original_url=model.original_url,
            created_at=model.created_at,
            updated_at=model.updated_at,
            expires_at=model.expires_at,
            click_count=model.click_count + extra_clicks,
        )

    @property
    def status(self) -> UrlStatus:
        if self.expires_at is not None and self.expires_at <= datetime.now(UTC):
            return UrlStatus.EXPIRED
        return UrlStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class RedirectResolution:
    """Result of resolving a code for redirect, plus how we resolved it."""

    target_url: str
    cache_state: CacheState


@dataclass(frozen=True, slots=True)
class UrlPage:
    items: list[UrlRecord]
    next_cursor: str | None


def _encode_cursor(record: UrlRecord) -> str:
    payload = json.dumps({"ts": record.created_at.isoformat(), "id": record.id})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, int] | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return datetime.fromisoformat(payload["ts"]), int(payload["id"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        # A malformed cursor is treated as "start from the beginning" rather
        # than a 400: cursors are opaque, and clients that mangle one should
        # still get a usable page.
        return None


class UrlService:
    def __init__(
        self,
        *,
        db: Database,
        cache: UrlCache,
        clicks: ClickTracker,
        settings: Settings,
    ) -> None:
        self._db = db
        self._cache = cache
        self._clicks = clicks
        self._settings = settings

    # ------------------------------------------------------------ create ---
    async def create(
        self,
        *,
        raw_url: str,
        expires_at: datetime | None = None,
        custom_alias: str | None = None,
    ) -> UrlRecord:
        """Create a shortened URL.

        Custom alias: a single attempt -- a taken alias is a 409 the user must
        resolve, not something to retry around.

        Generated code: up to `SHORT_CODE_MAX_ATTEMPTS` attempts. Each attempt
        is its own transaction, because a failed INSERT aborts the current one;
        PostgreSQL savepoints would avoid the reconnect but add a round trip to
        the overwhelmingly common success path, and collisions are rare enough
        (see app/services/short_code.py) that optimising the retry is pointless.
        """
        validated = validate_destination_url(raw_url, self._settings)

        if custom_alias is not None:
            if not self._settings.allow_custom_alias:
                raise CustomAliasDisabledError
            alias = validate_short_code(custom_alias, self._settings, is_custom_alias=True)
            record = await self._insert(alias, validated.normalized, expires_at, is_custom=True)
            urls_created_total.labels(alias_kind="custom").inc()
            return record

        attempts = self._settings.short_code_max_attempts
        for attempt in range(1, attempts + 1):
            code = generate_short_code(self._settings.short_code_length)
            try:
                record = await self._insert(code, validated.normalized, expires_at, is_custom=False)
            except ShortCodeConflictError:
                short_code_collisions_total.inc()
                logger.warning(
                    "short code collision; retrying",
                    extra={"attempt": attempt, "max_attempts": attempts},
                )
                continue
            urls_created_total.labels(alias_kind="generated").inc()
            return record

        short_code_generation_failures_total.inc()
        logger.error(
            "exhausted short code attempts",
            extra={"attempts": attempts, "code_length": self._settings.short_code_length},
        )
        raise ShortCodeGenerationError

    async def _insert(
        self,
        code: str,
        normalized_url: str,
        expires_at: datetime | None,
        *,
        is_custom: bool,
    ) -> UrlRecord:
        try:
            async with self._db.session() as session:
                url = await UrlRepository(session).insert(
                    short_code=code,
                    original_url=normalized_url,
                    expires_at=expires_at,
                )
                await session.commit()
                record = UrlRecord.from_model(url)
        except IntegrityError as exc:
            # The UNIQUE index on short_code did its job. For a custom alias
            # this is a user-visible conflict; for a generated code the caller
            # turns it into another attempt.
            if is_custom:
                raise AliasUnavailableError from exc
            raise ShortCodeConflictError("Short code collision.") from exc

        # Populate the cache eagerly. New links are overwhelmingly likely to be
        # clicked within seconds of creation (someone just pasted it somewhere),
        # so the first visitor should not pay for a cache miss.
        await self._cache.set(
            CachedUrl(
                id=record.id,
                short_code=record.short_code,
                original_url=record.original_url,
                created_at=record.created_at,
                expires_at=record.expires_at,
            )
        )
        logger.info(
            "short url created",
            extra={
                "short_code": record.short_code,
                "destination": safe_url(record.original_url),
                "has_expiry": record.expires_at is not None,
                "alias_kind": "custom" if is_custom else "generated",
            },
        )
        return record

    # ---------------------------------------------------------- redirect ---
    async def resolve_for_redirect(self, code: str) -> RedirectResolution:
        """Cache-aside resolution of a short code to its destination.

        1. Redis. A hit (or a negative hit) answers immediately.
        2. On miss/error, PostgreSQL, then populate Redis.
        3. Unknown codes get a negative-cache tombstone so a scanner cannot
           convert every request into a database query.
        """
        validate_short_code(code, self._settings)

        lookup = await self._cache.get(code)

        if lookup.state is CacheState.NEGATIVE_HIT:
            url_redirects_total.labels(result="not_found").inc()
            raise ShortCodeNotFoundError

        if lookup.state is CacheState.HIT and lookup.value is not None:
            cached = lookup.value
            if cached.is_expired():
                # The cached entry outlived the link (possible when TTL was
                # clamped by an expiry that has since passed). Drop it so the
                # next reader re-reads the authoritative row.
                await self._cache.invalidate(code, reason="expired")
                url_redirects_total.labels(result="expired").inc()
                raise ShortCodeExpiredError
            await self._register_click(code)
            url_redirects_total.labels(result="redirected").inc()
            return RedirectResolution(cached.original_url, CacheState.HIT)

        # Cache miss, cache error, or cache disabled -> authoritative read.
        async with self._db.session() as session:
            repo = UrlRepository(session)
            url = await repo.get_active_by_code(code)

            if url is None:
                await self._cache.set_missing(code)
                url_redirects_total.labels(result="not_found").inc()
                raise ShortCodeNotFoundError

            record = UrlRecord.from_model(url)
            if record.status is UrlStatus.EXPIRED:
                url_redirects_total.labels(result="expired").inc()
                raise ShortCodeExpiredError

            await self._cache.set(
                CachedUrl(
                    id=record.id,
                    short_code=record.short_code,
                    original_url=record.original_url,
                    created_at=record.created_at,
                    expires_at=record.expires_at,
                )
            )

            buffered = await self._clicks.record(code)
            if not buffered:
                # Redis refused the buffered increment (or buffering is off).
                # We already hold a session here, so persist directly.
                await repo.increment_click_count(code)
                await session.commit()

        url_redirects_total.labels(result="redirected").inc()
        return RedirectResolution(record.original_url, lookup.state)

    async def _register_click(self, code: str) -> None:
        """Count a click on the cache-hit path.

        There is no open session here, so if Redis cannot take the buffered
        increment we open one purely to record it. That only happens when Redis
        is broken (or buffering is disabled for a baseline benchmark), and
        silently dropping clicks would be worse than paying for a write while
        degraded. A failure to count never fails the redirect itself.
        """
        if await self._clicks.record(code):
            return
        try:
            async with self._db.session() as session:
                await UrlRepository(session).increment_click_count(code)
                await session.commit()
        except Exception:  # noqa: BLE001 - a click counter must never fail a redirect
            logger.warning("direct click increment failed", extra={"short_code": code})
            return
        clicks_recorded_total.labels(path="direct_db").inc()

    # ---------------------------------------------------------- metadata ---
    async def get_metadata(self, code: str) -> UrlRecord:
        """Read full metadata, including an accurate click count.

        Reads PostgreSQL rather than the cache: the cache intentionally does not
        hold `click_count`, and this endpoint's whole purpose is the counter.
        The un-flushed Redis delta is added so the number is correct between
        write-back cycles.
        """
        validate_short_code(code, self._settings)
        async with self._db.session() as session:
            url = await UrlRepository(session).get_active_by_code(code)
        if url is None:
            raise ShortCodeNotFoundError
        pending = await self._clicks.pending(code)
        return UrlRecord.from_model(url, extra_clicks=pending)

    async def list_recent(self, *, limit: int, cursor: str | None) -> UrlPage:
        decoded = _decode_cursor(cursor) if cursor else None
        async with self._db.session() as session:
            rows = await UrlRepository(session).list_recent(
                limit=limit + 1,
                cursor_created_at=decoded[0] if decoded else None,
                cursor_id=decoded[1] if decoded else None,
            )
        has_more = len(rows) > limit
        records = [UrlRecord.from_model(row) for row in rows[:limit]]
        return UrlPage(
            items=records,
            next_cursor=_encode_cursor(records[-1]) if has_more and records else None,
        )

    # ------------------------------------------------------------ delete ---
    async def delete(self, code: str) -> None:
        """Soft-delete a URL and invalidate its cache entry.

        Ordering is commit-then-invalidate. The opposite order (invalidate
        first) opens a window where a concurrent reader repopulates the cache
        from the not-yet-deleted row and the entry survives the delete. With
        this order the worst case is a brief 'not yet invalidated' window, which
        the invalidation immediately closes.
        """
        validate_short_code(code, self._settings)
        async with self._db.session() as session:
            deleted = await UrlRepository(session).soft_delete(code)
            await session.commit()

        if not deleted:
            raise ShortCodeNotFoundError

        await self._cache.invalidate(code, reason="deleted")
        await self._clicks.discard(code)
        urls_deleted_total.inc()
        logger.info("short url deleted", extra={"short_code": code})

    # ------------------------------------------------------------- sweep ---
    async def sweep_expired(self, *, limit: int = 500) -> int:
        """Retire expired links and evict their cache entries."""
        async with self._db.session() as session:
            codes = await UrlRepository(session).sweep_expired(now=datetime.now(UTC), limit=limit)
            await session.commit()
        for code in codes:
            await self._cache.invalidate(code, reason="expired")
        if codes:
            logger.info("swept expired urls", extra={"count": len(codes)})
        return len(codes)
