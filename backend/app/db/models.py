"""ORM models.

Schema rationale (long-form in docs/DATABASE_AND_SHARDING.md):

* `short_code` carries a UNIQUE constraint. This is not decoration -- it is the
  mechanism that makes concurrent short-code generation safe. Two API replicas
  that happen to generate the same code will both attempt an INSERT; PostgreSQL
  lets exactly one win and the loser retries with a fresh code.

* Deletes are soft (`deleted_at`). A hard delete would free the code for reuse,
  and because short links get pasted into chat logs and emails that live
  forever, a recycled code would silently point old traffic at a new
  destination. That is a link-hijacking vector, so codes are burned on delete.

* `click_count` is a plain counter column updated with `count = count + delta`,
  which is atomic in PostgreSQL. The redirect hot path does not touch it
  directly -- it buffers increments in Redis (see app/services/click_tracker.py).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

SHORT_CODE_MAX_LENGTH = 16
ORIGINAL_URL_MAX_LENGTH = 2048


class Url(Base):
    """A shortened URL."""

    __tablename__ = "urls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    short_code: Mapped[str] = mapped_column(
        String(SHORT_CODE_MAX_LENGTH),
        nullable=False,
        unique=True,
        comment="Base62 public identifier. UNIQUE -- also the collision guard.",
    )

    original_url: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Destination URL. Text + CHECK rather than VARCHAR(n) so the "
        "limit can move without an ALTER TYPE rewrite.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="NULL means the link never expires.",
    )

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Soft delete. Set means the code is burned and never reused.",
    )

    click_count: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )

    # Names here are the *suffix*; `NAMING_CONVENTION` in app/db/base.py adds
    # the `ck_urls_` prefix, so these must match the migration's full names.
    __table_args__ = (
        CheckConstraint(
            "click_count >= 0",
            name="click_count_non_negative",
        ),
        CheckConstraint(
            "char_length(original_url) BETWEEN 1 AND 2048",
            name="original_url_length",
        ),
        CheckConstraint(
            "short_code ~ '^[A-Za-z0-9_-]{4,16}$'",
            name="short_code_charset",
        ),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name="expires_after_created",
        ),
        # Supports `GET /api/v1/urls` (newest-first listing, keyset paginated).
        # DESC matches the query's ORDER BY so PostgreSQL can walk the index
        # backwards-free; id is the tiebreaker that makes the keyset stable.
        Index("ix_urls_created_at_id", text("created_at DESC"), text("id DESC")),
        # PARTIAL index for the expiry sweeper. Only rows that can actually
        # expire are indexed, so on a corpus where most links are permanent the
        # index stays a small fraction of the table size.
        Index(
            "ix_urls_expires_at_active",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Url id={self.id} short_code={self.short_code!r}>"
