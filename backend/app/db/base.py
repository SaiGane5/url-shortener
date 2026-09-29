"""SQLAlchemy declarative base."""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

#: Deterministic names for every constraint and index.
#:
#: Without this, SQLAlchemy lets PostgreSQL invent names for anything declared
#: implicitly -- `unique=True` on a column becomes `urls_short_code_key`, and a
#: primary key becomes `urls_pkey`. The Alembic migration names the same objects
#: `uq_urls_short_code` and `pk_urls`, so the two schema-construction paths
#: disagreed: a database built from this metadata (as the test suite does, for
#: speed) had different constraint names from one built by migration (as every
#: real deployment does).
#:
#: That drift has two concrete costs. `alembic revision --autogenerate` sees the
#: mismatch and emits spurious drop/create pairs for constraints that are
#: functionally identical; and any future migration that references a constraint
#: by name -- `op.drop_constraint("uq_urls_short_code", ...)` -- works in
#: production and fails in tests, or vice versa.
#:
#: `ck` intentionally passes the given name straight through, since all check
#: constraints are named explicitly and prefixing them here would double up
#: (`ck_urls_ck_urls_...`).
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models and by Alembic autogenerate."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
