"""Data access for the `urls` table.

The repository owns SQL and nothing else: no HTTP concepts, no caching, no
retry policy. That boundary is what makes the service layer testable against a
fake and keeps the "which index does this query use?" question answerable by
reading one file (see docs/DATABASE_AND_SHARDING.md).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, literal, select, text, tuple_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import dependency_latency_seconds
from app.db.models import Url


def _observe(operation: str, started: float) -> None:
    dependency_latency_seconds.labels(dependency="postgres", operation=operation).observe(
        time.perf_counter() - started
    )


def _rowcount(result: object) -> int:
    """Read `rowcount` from a DML result.

    `AsyncSession.execute` is typed as returning `Result`, but DML always
    produces a `CursorResult`, which is the only variant that exposes
    `rowcount`. The cast documents that invariant instead of hiding it behind a
    bare `type: ignore`.
    """
    return int(cast("CursorResult[Any]", result).rowcount or 0)


class UrlRepository:
    """Async data-access object for `Url`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------- reads ---
    async def get_active_by_code(self, short_code: str) -> Url | None:
        """Fetch a non-deleted URL by code.

        Index: `uq_urls_short_code` (unique b-tree). Equality on a unique key,
        so PostgreSQL does an Index Scan returning at most one row -- the
        `deleted_at IS NULL` predicate is then a filter on that single row
        rather than a selectivity problem.
        """
        started = time.perf_counter()
        stmt = select(Url).where(Url.short_code == short_code, Url.deleted_at.is_(None))
        result = await self._session.execute(stmt)
        _observe("get_active_by_code", started)
        return result.scalar_one_or_none()

    async def get_by_code_including_deleted(self, short_code: str) -> Url | None:
        started = time.perf_counter()
        result = await self._session.execute(select(Url).where(Url.short_code == short_code))
        _observe("get_by_code_including_deleted", started)
        return result.scalar_one_or_none()

    async def list_recent(
        self,
        *,
        limit: int,
        cursor_created_at: datetime | None = None,
        cursor_id: int | None = None,
    ) -> list[Url]:
        """Newest-first keyset pagination.

        Index: `ix_urls_created_at_id` on (created_at DESC, id DESC). Keyset
        (`WHERE (created_at, id) < (:ts, :id)`) rather than OFFSET, because
        OFFSET n makes PostgreSQL walk and discard n rows -- page 1000 costs
        1000x page 1. Keyset pagination is O(page size) at any depth.
        """
        started = time.perf_counter()
        stmt = select(Url).where(Url.deleted_at.is_(None))
        if cursor_created_at is not None and cursor_id is not None:
            stmt = stmt.where(
                tuple_(Url.created_at, Url.id)
                < tuple_(literal(cursor_created_at), literal(cursor_id))
            )
        stmt = stmt.order_by(Url.created_at.desc(), Url.id.desc()).limit(limit)
        result = await self._session.execute(stmt)
        _observe("list_recent", started)
        return list(result.scalars().all())

    async def count_active(self) -> int:
        started = time.perf_counter()
        result = await self._session.execute(
            select(func.count()).select_from(Url).where(Url.deleted_at.is_(None))
        )
        _observe("count_active", started)
        return int(result.scalar_one())

    # ------------------------------------------------------------ writes ---
    async def insert(
        self,
        *,
        short_code: str,
        original_url: str,
        expires_at: datetime | None,
    ) -> Url:
        """Insert one row.

        Raises `sqlalchemy.exc.IntegrityError` on a short-code collision. That
        is not an error path to hide -- it is how the caller learns to retry
        with a new code, and it is the only collision check that is correct
        under concurrency.
        """
        started = time.perf_counter()
        url = Url(short_code=short_code, original_url=original_url, expires_at=expires_at)
        self._session.add(url)
        await self._session.flush()
        _observe("insert", started)
        return url

    async def soft_delete(self, short_code: str) -> bool:
        """Mark a URL deleted. Returns False if it did not exist / was already gone.

        Soft delete keeps the code permanently reserved so it can never be
        reissued and silently redirect old traffic somewhere new.
        """
        started = time.perf_counter()
        stmt = (
            update(Url)
            .where(Url.short_code == short_code, Url.deleted_at.is_(None))
            .values(deleted_at=func.now(), updated_at=func.now())
        )
        result = await self._session.execute(stmt)
        _observe("soft_delete", started)
        return _rowcount(result) > 0

    async def increment_click_count(self, short_code: str, delta: int = 1) -> bool:
        """Atomically add `delta` clicks to one row.

        `click_count = click_count + :delta` is evaluated by PostgreSQL under a
        row lock, so concurrent increments cannot lose updates the way a
        read-modify-write in application code would.
        """
        started = time.perf_counter()
        stmt = (
            update(Url)
            .where(Url.short_code == short_code, Url.deleted_at.is_(None))
            .values(click_count=Url.click_count + delta)
        )
        result = await self._session.execute(stmt)
        _observe("increment_click_count", started)
        return _rowcount(result) > 0

    async def bulk_increment_click_counts(self, deltas: dict[str, int]) -> int:
        """Apply many buffered click deltas in one statement.

        Uses a VALUES list joined against `urls` so the whole flush is a single
        round trip and a single transaction: N separate UPDATEs would mean N
        round trips and N chances to leave the buffer half-applied.
        """
        if not deltas:
            return 0

        started = time.perf_counter()
        params: dict[str, Any] = {}
        rows: list[str] = []
        for index, (code, delta) in enumerate(deltas.items()):
            params[f"code_{index}"] = code
            params[f"delta_{index}"] = delta
            rows.append(f"(:code_{index}, CAST(:delta_{index} AS bigint))")

        # Note: `updated_at` is intentionally left alone. A click is not an edit
        # of the link, and touching it would make "when was this last modified"
        # useless and dirty every row on every flush.
        # ruff S608: the interpolated fragment contains only generated bind
        # parameter *placeholders* (`:code_0`, `:delta_0`), never values. Codes
        # and deltas travel in `params` and are bound by the driver.
        stmt = text(
            f"""
            UPDATE urls AS u
               SET click_count = u.click_count + v.delta
              FROM (VALUES {", ".join(rows)}) AS v(short_code, delta)
             WHERE u.short_code = v.short_code
               AND u.deleted_at IS NULL
            """  # noqa: S608
        )
        result = await self._session.execute(stmt, params)
        _observe("bulk_increment_click_counts", started)
        return _rowcount(result)

    async def sweep_expired(self, *, now: datetime, limit: int) -> list[str]:
        """Retire links whose expiry has passed. Returns the codes retired.

        Index: `ix_urls_expires_at_active`, a PARTIAL index on `expires_at`
        covering only rows with `expires_at IS NOT NULL AND deleted_at IS NULL`.
        The sweeper therefore touches only links that can actually expire,
        instead of scanning a table that is mostly permanent links.

        Expired rows are *soft*-deleted, same as user deletes: hard-deleting
        would return the code to the random keyspace, and a code that is later
        regenerated would point historical traffic at a brand-new destination.
        Bounded storage is handled by archival instead (see
        docs/PRODUCTION_READINESS.md).

        `LIMIT` + a subquery keeps each sweep a short transaction, so the job
        cannot hold locks long enough to interfere with live traffic.
        """
        started = time.perf_counter()
        candidates = (
            select(Url.id)
            .where(Url.expires_at.is_not(None), Url.expires_at <= now, Url.deleted_at.is_(None))
            .order_by(Url.expires_at)
            .limit(limit)
            .scalar_subquery()
        )
        stmt = (
            update(Url)
            .where(Url.id.in_(candidates))
            .values(deleted_at=func.now(), updated_at=func.now())
            .returning(Url.short_code)
        )
        result = await self._session.execute(stmt)
        _observe("sweep_expired", started)
        return [row[0] for row in result.fetchall()]
