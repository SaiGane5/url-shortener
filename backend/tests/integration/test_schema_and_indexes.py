"""Database schema contract: constraints, indexes, and migration fidelity.

Two things are pinned here.

**Constraints.** They are the last line of defence for correctness. The
UNIQUE index on `short_code` is what makes concurrent code generation safe (see
test_concurrency.py), and the CHECK constraints stop a bug in the service layer
from writing a negative click count or an expiry that precedes creation. If any
of them are dropped, application-level tests would keep passing while the real
guarantee quietly disappeared -- so they are asserted directly.

**Migration fidelity.** `conftest` builds the test schema from ORM metadata for
speed, but production is built by Alembic. If those two drift, every test here
passes against a schema that no deployment ever has. The last test applies the
real migration to a throwaway database and diffs it against the metadata.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.db.base import Base
from app.db.session import Database
from tests.conftest import TEST_DATABASE_URL, _admin_url

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _admin_engine() -> Any:
    """Engine on the maintenance database, for CREATE/DROP DATABASE."""
    return create_async_engine(
        _admin_url(TEST_DATABASE_URL),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )


async def _create_database(name: str) -> None:
    engine = _admin_engine()
    try:
        async with engine.connect() as conn:
            # Identifiers cannot be bound parameters, and `name` is a locally
            # generated `uuid4` hex string -- never caller-supplied.
            await conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    finally:
        await engine.dispose()


async def _drop_database(name: str) -> None:
    engine = _admin_engine()
    try:
        async with engine.connect() as conn:
            # DROP fails while the migration's connection is still winding
            # down, so evict any stragglers first.
            await conn.exec_driver_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "  # noqa: S608
                f"WHERE datname = '{name}'"
            )
            await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        await engine.dispose()


def _diff_target(entry: Any) -> str | None:
    """Name of the object an Alembic diff entry refers to, if it has one.

    Entries are either `(op, object)` tuples or lists of them, depending on
    the kind of change, so this stays defensive rather than indexing blindly.
    """
    if isinstance(entry, tuple) and len(entry) > 1:
        return getattr(entry[1], "name", None)
    return None


pytestmark = [pytest.mark.integration]

EXPECTED_INDEXES = {
    "pk_urls",
    "uq_urls_short_code",
    "ix_urls_created_at_id",
    "ix_urls_expires_at_active",
}


async def fetch_indexes(database: Database) -> dict[str, str]:
    async with database.session() as session:
        result = await session.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = 'urls' AND schemaname = current_schema()"
            )
        )
        return {row.indexname: row.indexdef for row in result}


async def fetch_constraints(database: Database) -> dict[str, str]:
    async with database.session() as session:
        result = await session.execute(
            text(
                "SELECT con.conname, pg_get_constraintdef(con.oid) AS definition "
                "FROM pg_constraint con "
                "JOIN pg_class rel ON rel.oid = con.conrelid "
                "WHERE rel.relname = 'urls'"
            )
        )
        return {row.conname: row.definition for row in result}


class TestIndexes:
    async def test_expected_indexes_exist_and_no_others(self, database: Database) -> None:
        """Pins the set deliberately, in both directions.

        Missing index -> a query silently degrades to a sequential scan.
        Extra index -> an unjustified write-amplification cost that nobody
        remembers adding. docs/DATABASE_AND_SHARDING.md justifies each one.
        """
        assert set(await fetch_indexes(database)) == EXPECTED_INDEXES

    async def test_short_code_index_is_unique(self, database: Database) -> None:
        """The single most important index in the system.

        It serves the redirect lookup *and* arbitrates concurrent inserts. A
        non-unique index would make short-code collision handling a race.
        """
        definition = (await fetch_indexes(database))["uq_urls_short_code"]

        assert "UNIQUE" in definition
        assert "short_code" in definition

    async def test_listing_index_matches_the_listing_query_order(self, database: Database) -> None:
        """`(created_at DESC, id DESC)` must match `ORDER BY` exactly.

        A plain ascending index still avoids a sort here (PostgreSQL can scan
        backwards), but the composite trailing `id` is what makes keyset
        pagination total: `created_at` alone is not unique, so rows sharing a
        timestamp could be skipped or repeated across pages.
        """
        definition = (await fetch_indexes(database))["ix_urls_created_at_id"]

        assert "created_at DESC" in definition
        assert "id DESC" in definition

    async def test_expiry_index_is_partial(self, database: Database) -> None:
        """Most links never expire, so a full index would be mostly dead weight.

        The partial predicate keeps it proportional to the number of rows the
        sweeper actually cares about, and excludes already-deleted rows.
        """
        definition = (await fetch_indexes(database))["ix_urls_expires_at_active"]

        assert "WHERE" in definition
        assert "expires_at IS NOT NULL" in definition
        assert "deleted_at IS NULL" in definition


class TestConstraints:
    async def test_short_code_is_unique(self, database: Database) -> None:
        constraints = await fetch_constraints(database)
        assert "uq_urls_short_code" in constraints

    async def test_click_count_cannot_go_negative(self, database: Database) -> None:
        """Guards the click write-back path.

        A bug in the flusher that applied a delta twice, or with the wrong
        sign, would corrupt analytics silently. Better to fail the write.
        """
        async with database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO urls (short_code, original_url, created_at, updated_at, "
                    "click_count) VALUES ('negchk1', 'https://example.com/x', now(), now(), 0)"
                )
            )
            await session.commit()

        with pytest.raises(Exception, match="ck_urls_click_count_non_negative"):
            async with database.session() as session:
                await session.execute(
                    text("UPDATE urls SET click_count = -1 WHERE short_code = 'negchk1'")
                )
                await session.commit()

    async def test_expiry_cannot_precede_creation(self, database: Database) -> None:
        with pytest.raises(Exception, match="ck_urls_expires_after_created"):
            async with database.session() as session:
                await session.execute(
                    text(
                        "INSERT INTO urls (short_code, original_url, created_at, updated_at, "
                        "expires_at, click_count) VALUES ('expchk1', "
                        "'https://example.com/x', now(), now(), now() - interval '1 hour', 0)"
                    )
                )
                await session.commit()

    async def test_original_url_cannot_be_empty(self, database: Database) -> None:
        with pytest.raises(Exception, match="ck_urls"):
            async with database.session() as session:
                await session.execute(
                    text(
                        "INSERT INTO urls (short_code, original_url, created_at, updated_at, "
                        "click_count) VALUES ('emptychk', '', now(), now(), 0)"
                    )
                )
                await session.commit()

    async def test_short_code_charset_is_enforced_in_the_database(self, database: Database) -> None:
        """Defence in depth behind `validate_short_code`.

        A code containing `/` would break routing; the database refuses it
        even if a future code path forgets to validate.
        """
        with pytest.raises(Exception, match="ck_urls"):
            async with database.session() as session:
                await session.execute(
                    text(
                        "INSERT INTO urls (short_code, original_url, created_at, updated_at, "
                        "click_count) VALUES ('bad/code', 'https://example.com/x', "
                        "now(), now(), 0)"
                    )
                )
                await session.commit()


class TestQueryPlans:
    async def test_redirect_lookup_uses_the_unique_index(self, database: Database) -> None:
        """The hot path must never be a sequential scan.

        On a table this small PostgreSQL would legitimately prefer a seq scan,
        so `enable_seqscan` is disabled for the check: the point is that a
        usable index *exists and is chosen when it matters*, not that the
        planner picks it at 20 rows. docs/DATABASE_AND_SHARDING.md carries the
        EXPLAIN ANALYZE output at realistic volume.
        """
        async with database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO urls (short_code, original_url, created_at, updated_at, "
                    "click_count) VALUES ('planchk', 'https://example.com/x', now(), now(), 0)"
                )
            )
            await session.commit()

        async with database.session() as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            result = await session.execute(
                text(
                    "EXPLAIN (FORMAT TEXT) SELECT id, short_code, original_url, created_at, "
                    "updated_at, expires_at, click_count FROM urls "
                    "WHERE short_code = 'planchk' AND deleted_at IS NULL"
                )
            )
            plan = "\n".join(row[0] for row in result)

        assert "uq_urls_short_code" in plan, plan
        assert "Index Scan" in plan or "Index Only Scan" in plan, plan

    async def test_listing_query_avoids_a_sort(self, database: Database) -> None:
        """`ORDER BY created_at DESC, id DESC` should read the index in order.

        A `Sort` node here means the whole table is materialised and sorted on
        every page of the listing endpoint.
        """
        async with database.session() as session:
            for index in range(5):
                await session.execute(
                    text(
                        "INSERT INTO urls (short_code, original_url, created_at, updated_at, "
                        "click_count) VALUES (:code, 'https://example.com/x', "
                        "now(), now(), 0)"
                    ),
                    {"code": f"sortc{index:02d}"},
                )
            await session.commit()

        async with database.session() as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            result = await session.execute(
                text(
                    "EXPLAIN (FORMAT TEXT) SELECT id, short_code FROM urls "
                    "WHERE deleted_at IS NULL ORDER BY created_at DESC, id DESC LIMIT 20"
                )
            )
            plan = "\n".join(row[0] for row in result)

        assert "ix_urls_created_at_id" in plan, plan
        assert "Sort Key" not in plan, f"listing query is sorting rather than scanning:\n{plan}"


class TestMigrationFidelity:
    """The migration and the ORM metadata must describe the same schema.

    The test suite builds its schema from metadata for speed; every real
    deployment builds it with Alembic. If they drift, the whole suite can pass
    against a schema that exists nowhere in production.
    """

    async def test_object_names_match_the_migration(self, database: Database) -> None:
        """Regression: implicit constraints were auto-named by PostgreSQL.

        `unique=True` on the column produced `urls_short_code_key` in
        metadata while the migration created `uq_urls_short_code`, and the
        primary key was `urls_pkey` vs `pk_urls`. A naming convention on the
        metadata (app/db/base.py) now makes both paths agree, which is what
        keeps `alembic revision --autogenerate` from emitting spurious
        drop/create pairs.
        """
        indexes = set(await fetch_indexes(database))
        constraints = set(await fetch_constraints(database))

        assert "uq_urls_short_code" in indexes | constraints
        assert "pk_urls" in indexes | constraints
        assert "urls_short_code_key" not in indexes | constraints
        assert "urls_pkey" not in indexes | constraints

    async def test_check_constraint_names_match_the_migration(self, database: Database) -> None:
        constraints = set(await fetch_constraints(database))

        for expected in (
            "ck_urls_click_count_non_negative",
            "ck_urls_original_url_length",
            "ck_urls_short_code_charset",
            "ck_urls_expires_after_created",
        ):
            assert expected in constraints, f"{expected} missing; migration and ORM disagree"

    async def test_metadata_matches_the_migrated_schema(self) -> None:
        """Applies the real migration to a throwaway database and diffs it.

        This is the authoritative check: anything the name-based tests above
        miss -- a column type, a nullability, a server default -- surfaces
        here as a diff.

        Alembic is invoked as a subprocess, exactly as the `migrate` service
        does in docker-compose, because `alembic/env.py` calls `asyncio.run()`
        and so cannot be driven from inside an already-running event loop. The
        comparison then goes through `run_sync`, which hands Alembic the
        synchronous connection it requires without pulling in a second
        database driver purely for tests.
        """
        scratch = f"urlshort_migr_{uuid.uuid4().hex[:8]}"
        scratch_url = f"{TEST_DATABASE_URL.rsplit('/', 1)[0]}/{scratch}"

        await _create_database(scratch)
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "alembic",
                "upgrade",
                "head",
                cwd=BACKEND_ROOT,
                env={**os.environ, "DATABASE_URL": scratch_url},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
            assert process.returncode == 0, f"alembic upgrade failed:\n{stderr.decode()}"

            engine = create_async_engine(scratch_url, poolclass=NullPool)
            try:
                async with engine.connect() as conn:
                    diff = await conn.run_sync(
                        lambda sync_conn: compare_metadata(
                            MigrationContext.configure(sync_conn), Base.metadata
                        )
                    )
            finally:
                await engine.dispose()

            # Alembic's own bookkeeping table is not part of our metadata.
            meaningful = [entry for entry in diff if _diff_target(entry) != "alembic_version"]
            assert meaningful == [], f"migration and ORM metadata differ:\n{meaningful}"
        finally:
            await _drop_database(scratch)
