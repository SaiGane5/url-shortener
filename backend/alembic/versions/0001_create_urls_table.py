"""Create the urls table with its constraints and indexes.

Index rationale (measured EXPLAIN ANALYZE output in docs/DATABASE_AND_SHARDING.md):

1. uq_urls_short_code -- UNIQUE b-tree on short_code
   Query:      SELECT ... FROM urls WHERE short_code = $1 AND deleted_at IS NULL
   Also:       the uniqueness guard that makes concurrent code generation safe.
   Trade-off:  one extra b-tree to maintain per INSERT. Unavoidable, because the
               constraint is required for correctness regardless of read speed.
   vs scan:    equality on a unique key is O(log n) and returns <=1 row; a Seq
               Scan is O(n) and would dominate the redirect path.

2. ix_urls_created_at_id -- (created_at DESC, id DESC)
   Query:      the newest-first listing behind GET /api/v1/urls, keyset paginated.
   Trade-off:  writes are append-shaped (created_at is always "now"), so all
               inserts hit the rightmost index page -- cheap, but that page is a
               contention point at very high write rates.
   vs scan:    without it, every listing sorts the whole table.

3. ix_urls_expires_at_active -- PARTIAL (expires_at) WHERE expires_at IS NOT NULL
                               AND deleted_at IS NULL
   Query:      the expiry sweeper.
   Why partial: most links never expire. Indexing only rows that *can* expire
               keeps this index a small fraction of the table and means
               permanent links cost nothing to maintain here.
   vs scan:    a full scan per sweep interval would compete with live traffic.

Deliberately NOT indexed: original_url. Nothing looks a URL up by destination,
and a b-tree over 2KB text would be large and write-amplifying. If "find my
existing link for this URL" ever ships, the right tool is an index on a
hash of the URL, not on the URL itself.

Revision ID: 0001
Revises:
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "urls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "short_code",
            sa.String(length=16),
            nullable=False,
            comment="Base62 public identifier. UNIQUE -- also the collision guard.",
        ),
        sa.Column(
            "original_url",
            sa.Text(),
            nullable=False,
            comment=(
                "Destination URL. Text + CHECK rather than VARCHAR(n) so the limit "
                "can move without an ALTER TYPE rewrite."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="NULL means the link never expires.",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Soft delete. Set means the code is burned and never reused.",
        ),
        sa.Column("click_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_urls"),
        sa.UniqueConstraint("short_code", name="uq_urls_short_code"),
        # Suffixes only. The `ck` naming convention in app/db/base.py prepends
        # `ck_urls_`, so spelling the full name here would produce
        # `ck_urls_ck_urls_...` and permanently diverge from the ORM metadata.
        sa.CheckConstraint("click_count >= 0", name="click_count_non_negative"),
        sa.CheckConstraint(
            "char_length(original_url) BETWEEN 1 AND 2048",
            name="original_url_length",
        ),
        sa.CheckConstraint(
            "short_code ~ '^[A-Za-z0-9_-]{4,16}$'",
            name="short_code_charset",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name="expires_after_created",
        ),
    )

    op.create_index(
        "ix_urls_created_at_id",
        "urls",
        [sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
    )

    op.create_index(
        "ix_urls_expires_at_active",
        "urls",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("expires_at IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_urls_expires_at_active", table_name="urls")
    op.drop_index("ix_urls_created_at_id", table_name="urls")
    op.drop_table("urls")
