"""Concurrency and correctness under simultaneous load.

Every test here fires genuinely overlapping work at real PostgreSQL and Redis.
The failures being hunted are the ones that only appear when two requests
interleave: a lost click, two links sharing a code, a delete that races a
cache refill.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.db.session import Database
from app.repositories.url_repository import UrlRepository
from app.services.url_service import UrlService

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]


class TestConcurrentCreation:
    async def test_parallel_creates_all_succeed_with_unique_codes(
        self, client: AsyncClient
    ) -> None:
        responses = await asyncio.gather(
            *(
                client.post("/api/v1/urls", json={"url": f"https://example.com/parallel/{index}"})
                for index in range(40)
            )
        )

        assert all(response.status_code == 201 for response in responses)
        codes = [response.json()["short_code"] for response in responses]
        assert len(set(codes)) == 40, "two concurrent creates produced the same code"

    async def test_parallel_creates_of_the_same_url_are_independent(
        self, client: AsyncClient
    ) -> None:
        responses = await asyncio.gather(
            *(
                client.post("/api/v1/urls", json={"url": "https://example.com/identical"})
                for _ in range(20)
            )
        )

        assert all(response.status_code == 201 for response in responses)
        assert len({response.json()["short_code"] for response in responses}) == 20

    async def test_only_one_writer_wins_a_contested_alias(self, client: AsyncClient) -> None:
        """The UNIQUE index is the arbiter, not application-level checking.

        A check-then-insert would let several of these pass the check before
        any of them inserts. Exactly one 201 and the rest 409 proves the
        database is deciding.
        """
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/v1/urls",
                    json={"url": f"https://example.com/racer/{index}", "custom_alias": "contested"},
                )
                for index in range(15)
            )
        )
        statuses = [response.status_code for response in responses]

        assert statuses.count(201) == 1, f"expected exactly one winner, got {statuses}"
        assert statuses.count(409) == 14

    async def test_collision_retry_yields_a_fresh_code(
        self, database: Database, url_service: UrlService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Forces the retry branch that random collisions make unobservable.

        The generator is stubbed to return one fixed code twice and then a
        distinct one, so the first insert wins, the second hits the UNIQUE
        index, and the retry must succeed rather than surfacing the conflict.
        """
        codes = iter(["dupe123", "dupe123", "uniq456"])
        monkeypatch.setattr(
            "app.services.url_service.generate_short_code",
            lambda _length: next(codes),
        )

        first = await url_service.create(raw_url="https://example.com/one")
        second = await url_service.create(raw_url="https://example.com/two")

        assert first.short_code == "dupe123"
        assert second.short_code == "uniq456"

    async def test_gives_up_after_max_attempts(
        self, url_service: UrlService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exhausting the retry budget must be a clean 503, not a hang.

        In practice this means the keyspace is saturated and
        SHORT_CODE_LENGTH needs to grow -- a condition worth surfacing
        loudly rather than retrying forever.
        """
        from app.core.errors import ShortCodeGenerationError

        monkeypatch.setattr(
            "app.services.url_service.generate_short_code",
            lambda _length: "always1",
        )

        await url_service.create(raw_url="https://example.com/first")
        with pytest.raises(ShortCodeGenerationError):
            await url_service.create(raw_url="https://example.com/second")


class TestConcurrentClicks:
    async def test_no_clicks_are_lost_under_parallel_redirects(self, app_factory: Any) -> None:
        """Read-modify-write on a counter is the classic lost-update bug.

        100 concurrent redirects must produce exactly 100 clicks. Redis INCR
        makes each increment atomic; a naive `count = count + 1` in Python
        would lose most of them.
        """
        client = await app_factory(click_flush_enabled=True)
        created = await client.post("/api/v1/urls", json={"url": "https://example.com/hot"})
        code = created.json()["short_code"]

        responses = await asyncio.gather(*(client.get(f"/{code}") for _ in range(100)))
        assert all(response.status_code == 307 for response in responses)

        metadata = await client.get(f"/api/v1/urls/{code}")
        assert metadata.json()["click_count"] == 100

    async def test_buffered_counts_reach_postgresql(
        self, app_factory: Any, database: Database
    ) -> None:
        """The buffered count must land in the durable row, not just Redis.

        Asserted by reading the row directly rather than through the API,
        because the metadata endpoint deliberately adds any still-pending
        Redis delta -- which would mask a flusher that never ran.
        """
        client = await app_factory(
            click_flush_enabled=True,
            click_flush_interval_seconds=0.5,
        )
        created = await client.post("/api/v1/urls", json={"url": "https://example.com/flushed"})
        code = created.json()["short_code"]

        await asyncio.gather(*(client.get(f"/{code}") for _ in range(25)))

        # Give the background flusher a couple of intervals to drain.
        deadline = asyncio.get_running_loop().time() + 5.0
        stored = 0
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.25)
            async with database.session() as session:
                result = await session.execute(
                    text("SELECT click_count FROM urls WHERE short_code = :code"),
                    {"code": code},
                )
                stored = result.scalar_one()
            if stored == 25:
                break

        assert stored == 25, f"only {stored} of 25 clicks reached PostgreSQL"

    async def test_direct_increments_are_atomic_without_the_buffer(self, app_factory: Any) -> None:
        """Baseline mode writes straight to PostgreSQL.

        The UPDATE must use `click_count = click_count + 1` in SQL; computing
        the new value in Python would lose updates here.
        """
        client = await app_factory(click_flush_enabled=False)
        created = await client.post("/api/v1/urls", json={"url": "https://example.com/direct"})
        code = created.json()["short_code"]

        await asyncio.gather(*(client.get(f"/{code}") for _ in range(50)))

        metadata = await client.get(f"/api/v1/urls/{code}")
        assert metadata.json()["click_count"] == 50

    async def test_repository_increment_is_atomic(self, database: Database) -> None:
        """Tests the SQL directly, bypassing the Redis buffer entirely."""
        async with database.session() as session:
            await UrlRepository(session).insert(
                short_code="atomic1",
                original_url="https://example.com/x",
                expires_at=None,
            )
            await session.commit()

        async def bump() -> None:
            async with database.session() as session:
                await UrlRepository(session).increment_click_count("atomic1", delta=1)
                await session.commit()

        await asyncio.gather(*(bump() for _ in range(30)))

        async with database.session() as session:
            result = await session.execute(
                text("SELECT click_count FROM urls WHERE short_code = :code"),
                {"code": "atomic1"},
            )
            assert result.scalar_one() == 30


class TestConcurrentDeleteAndRead:
    async def test_delete_racing_reads_never_leaves_a_resolvable_link(
        self, client: AsyncClient
    ) -> None:
        """The dangerous interleaving: a refill lands after the invalidation.

        Reads run alongside the delete; some may legitimately still succeed
        (they were in flight). What must never happen is the link resolving
        *after* the dust settles, which is what a late cache refill causes.
        """
        created = await client.post("/api/v1/urls", json={"url": "https://example.com/racy"})
        code = created.json()["short_code"]

        async def read() -> int:
            return (await client.get(f"/{code}")).status_code

        async def delete() -> int:
            return (await client.delete(f"/api/v1/urls/{code}")).status_code

        await asyncio.gather(*[read() for _ in range(20)], delete(), *[read() for _ in range(20)])

        # Settle, then assert the terminal state is unambiguous.
        await asyncio.sleep(0.2)
        for _ in range(5):
            assert (await client.get(f"/{code}")).status_code == 404

    async def test_parallel_deletes_produce_one_success(self, client: AsyncClient) -> None:
        created = await client.post("/api/v1/urls", json={"url": "https://example.com/x"})
        code = created.json()["short_code"]

        statuses = await asyncio.gather(*(client.delete(f"/api/v1/urls/{code}") for _ in range(10)))
        codes = [response.status_code for response in statuses]

        assert codes.count(204) == 1, f"delete is not idempotent-safe: {codes}"
        assert codes.count(404) == 9


class TestConcurrentCachePopulation:
    async def test_simultaneous_cold_reads_agree(self, client: AsyncClient) -> None:
        """Cache stampede on a cold key.

        Many readers miss at once and all refill. Every one must get the
        correct destination; none may see a partial or empty entry.
        """
        created = await client.post("/api/v1/urls", json={"url": "https://example.com/cold"})
        code = created.json()["short_code"]

        responses = await asyncio.gather(*(client.get(f"/{code}") for _ in range(50)))

        assert {response.status_code for response in responses} == {307}
        assert {response.headers["location"] for response in responses} == {
            "https://example.com/cold"
        }

    async def test_mixed_workload_stays_consistent(self, client: AsyncClient) -> None:
        """Creates, reads, and metadata calls interleaved.

        A broad smoke test for cross-operation interference: nothing should
        5xx, and every created link should be resolvable afterwards.
        """
        created = await client.post("/api/v1/urls", json={"url": "https://example.com/mixed"})
        existing = created.json()["short_code"]

        operations: list[Any] = []
        for index in range(15):
            operations.append(
                client.post("/api/v1/urls", json={"url": f"https://example.com/mixed/{index}"})
            )
            operations.append(client.get(f"/{existing}"))
            operations.append(client.get(f"/api/v1/urls/{existing}"))

        responses = await asyncio.gather(*operations)
        assert all(response.status_code < 500 for response in responses)

        new_codes = [
            response.json()["short_code"] for response in responses if response.status_code == 201
        ]
        assert len(set(new_codes)) == 15
        for code in new_codes:
            assert (await client.get(f"/{code}")).status_code == 307
