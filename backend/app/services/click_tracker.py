"""Click counting via a Redis write-back buffer.

The problem
-----------
The naive implementation makes every redirect do `UPDATE urls SET click_count =
click_count + 1`. That turns the cheapest, most-cached, highest-volume endpoint
in the system into a *write* against a single row -- so a popular link
serialises all of its traffic behind one row lock, and the Redis cache we added
to avoid reading PostgreSQL is defeated by still writing to it.

The approach
------------
Redirects `INCR` a per-code counter in Redis (atomic, ~50us, no lock contention
across codes) and register the code in a "dirty" set. A background task on each
replica periodically claims dirty codes and folds their deltas into PostgreSQL
with one batched UPDATE.

Correctness under multiple replicas
-----------------------------------
Claiming is done with `SPOP`, which atomically removes members -- so if two
replicas flush simultaneously they get *disjoint* sets of codes and cannot
double-apply a delta. Reading the delta uses `GETDEL`, which atomically reads
and clears, so clicks arriving during a flush accumulate into the next cycle
rather than being lost. If the PostgreSQL write then fails, the deltas are
added back with `INCRBY` and the codes re-registered.

Durability trade-off (stated plainly)
-------------------------------------
Counts are eventually consistent, with a lag of up to
`CLICK_FLUSH_INTERVAL_SECONDS`. If Redis loses data between a click and a
flush, those clicks are lost. That is an accepted trade for a vanity metric:
analytics counters are not billing records. `GET /api/v1/urls/{code}` hides the
lag by adding the un-flushed Redis delta to the persisted count, so users see a
correct number even mid-cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Final

from redis.exceptions import RedisError

from app.cache.redis_client import RedisGateway
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import (
    click_flush_failures_total,
    clicks_flushed_total,
    clicks_recorded_total,
    redis_errors_total,
)
from app.db.session import Database
from app.repositories.url_repository import UrlRepository

logger = get_logger(__name__)

_PENDING_PREFIX: Final = "clicks:pending"
_DIRTY_SET: Final = "clicks:dirty"


def _as_code_list(claimed: object) -> list[str]:
    """Normalise SPOP's reply shape.

    `SPOP key` returns a single member while `SPOP key count` returns a
    collection, and redis-py types the union accordingly.
    """
    if claimed is None:
        return []
    if isinstance(claimed, str):
        return [claimed]
    if isinstance(claimed, bytes):
        return [claimed.decode("utf-8", errors="replace")]
    if isinstance(claimed, list | set | tuple):
        return [item.decode("utf-8") if isinstance(item, bytes) else str(item) for item in claimed]
    return []


class ClickTracker:
    """Buffers click increments in Redis and flushes them to PostgreSQL."""

    def __init__(self, redis: RedisGateway, db: Database, settings: Settings) -> None:
        self._redis = redis
        self._db = db
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------- keys ---
    @staticmethod
    def pending_key(short_code: str) -> str:
        return f"{_PENDING_PREFIX}:{short_code}"

    @property
    def dirty_set_key(self) -> str:
        return _DIRTY_SET

    # ------------------------------------------------------------ record ---
    async def record(self, short_code: str) -> bool:
        """Buffer one click. Returns False if Redis could not accept it.

        The INCR and the SADD go out as one MULTI/EXEC so the counter and its
        dirty-set registration cannot diverge: an incremented counter that was
        never registered would sit un-flushed until the link was clicked again.
        """
        if not self._settings.click_flush_enabled:
            return False
        try:
            async with self._redis.track("click_record"):
                pipe = self._redis.client.pipeline(transaction=True)
                pipe.incr(self.pending_key(short_code))
                pipe.sadd(_DIRTY_SET, short_code)
                await pipe.execute()
        except (RedisError, OSError) as exc:
            redis_errors_total.labels(operation="click_record").inc()
            logger.warning("click buffering failed", extra={"reason": type(exc).__name__})
            return False
        else:
            clicks_recorded_total.labels(path="redis_buffer").inc()
            return True

    async def pending(self, short_code: str) -> int:
        """Un-flushed click delta for one code. Never raises (0 on failure)."""
        try:
            async with self._redis.track("click_pending"):
                raw = await self._redis.client.get(self.pending_key(short_code))
        except (RedisError, OSError):
            redis_errors_total.labels(operation="click_pending").inc()
            return 0
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0

    async def discard(self, short_code: str) -> None:
        """Drop the buffer for a code (used on delete). Never raises."""
        try:
            async with self._redis.track("click_discard"):
                pipe = self._redis.client.pipeline(transaction=True)
                pipe.delete(self.pending_key(short_code))
                pipe.srem(_DIRTY_SET, short_code)
                await pipe.execute()
        except (RedisError, OSError):
            redis_errors_total.labels(operation="click_discard").inc()

    # ------------------------------------------------------------- flush ---
    async def flush_once(self) -> int:
        """Claim and persist one batch of buffered clicks. Returns clicks applied."""
        try:
            async with self._redis.track("click_claim"):
                claimed = await self._redis.client.spop(
                    _DIRTY_SET, self._settings.click_flush_batch_size
                )
            codes = _as_code_list(claimed)
        except (RedisError, OSError) as exc:
            redis_errors_total.labels(operation="click_claim").inc()
            logger.warning("click flush claim failed", extra={"reason": type(exc).__name__})
            return 0

        if not codes:
            return 0

        try:
            async with self._redis.track("click_drain"):
                pipe = self._redis.client.pipeline(transaction=True)
                for code in codes:
                    pipe.getdel(self.pending_key(code))
                raw_deltas = await pipe.execute()
        except (RedisError, OSError) as exc:
            redis_errors_total.labels(operation="click_drain").inc()
            # Counters were not read, so only the claim needs undoing.
            await self._requeue(codes, deltas=None)
            logger.warning("click flush drain failed", extra={"reason": type(exc).__name__})
            return 0

        deltas: dict[str, int] = {}
        for code, raw in zip(codes, raw_deltas, strict=True):
            try:
                delta = int(raw) if raw is not None else 0
            except (TypeError, ValueError):
                delta = 0
            if delta > 0:
                deltas[code] = delta

        if not deltas:
            return 0

        try:
            async with self._db.session() as session:
                await UrlRepository(session).bulk_increment_click_counts(deltas)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - any failure at all must requeue the deltas
            click_flush_failures_total.inc()
            await self._requeue(list(deltas), deltas=deltas)
            logger.error(
                "click flush to postgres failed; deltas requeued",
                extra={"codes": len(deltas), "reason": type(exc).__name__},
            )
            return 0

        total = sum(deltas.values())
        clicks_flushed_total.inc(total)
        logger.debug("flushed click deltas", extra={"codes": len(deltas), "clicks": total})
        return total

    async def _requeue(self, codes: list[str], *, deltas: dict[str, int] | None) -> None:
        """Put claimed work back so a failed flush does not drop clicks."""
        try:
            async with self._redis.track("click_requeue"):
                pipe = self._redis.client.pipeline(transaction=True)
                if deltas:
                    for code, delta in deltas.items():
                        pipe.incrby(self.pending_key(code), delta)
                if codes:
                    pipe.sadd(_DIRTY_SET, *codes)
                await pipe.execute()
        except (RedisError, OSError):
            redis_errors_total.labels(operation="click_requeue").inc()
            logger.error("click requeue failed; buffered clicks lost", extra={"codes": len(codes)})

    # -------------------------------------------------- background worker ---
    async def _run(self) -> None:
        interval = self._settings.click_flush_interval_seconds
        logger.info("click flusher started", extra={"interval_seconds": interval})
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            try:
                await self.flush_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                click_flush_failures_total.inc()
                logger.exception("unexpected error in click flusher")

    def start(self) -> None:
        if not self._settings.click_flush_enabled or self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="click-flusher")

    async def stop(self) -> None:
        """Stop the worker and drain whatever is still buffered.

        Draining on shutdown is what keeps a rolling deploy from discarding up
        to one interval's worth of clicks on every replica.
        """
        self._stopping.set()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=5.0)
        with contextlib.suppress(Exception):
            await self.flush_once()
        logger.info("click flusher stopped")
