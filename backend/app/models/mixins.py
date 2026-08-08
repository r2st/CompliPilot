"""Column types and mixins shared by every model.

Three decisions live here because getting them wrong is expensive in every
table that repeats them:

* **Money is paise.** Every monetary column is a ``BigInteger`` count of paise
  — never a float, never rupees. Penalties, turnover thresholds and plan fees
  all cross the API as integers, and the only place a decimal point is inserted
  is the UI.
* **Tenancy.** ``organization_id`` is non-null on every tenant-owned table, so
  a row cannot be written without a tenant and every tenant-filtered query has
  an index to use.
* **Soft delete.** Compliance data is never hard-deleted. A filing can be
  reopened years later during an assessment, and a row that is gone cannot be
  explained to an officer.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, declared_attr, mapped_column
from sqlalchemy.types import JSON

# JSONB on Postgres (indexable, typed) and plain JSON on SQLite (tests).
JSONType = JSON().with_variant(JSONB, "postgresql")

# Money, as an integer count of paise. ₹1 is 100. The type is aliased rather
# than written inline so that "is this column money?" is answerable by reading
# the model, and so a future change of width happens in one place.
Paise = BigInteger

# A signed 64-bit column tops out around 9.2e18 paise. That is far past any
# real figure, but the bound is declared so callers can validate against it
# rather than discovering it as an INSERT failure — Postgres refuses the write
# while SQLite silently keeps whatever it was handed, and neither of those is
# where a caller should find out.
PAISE_MAX = 9_223_372_036_854_775_807

RUPEE = 100


def utcnow() -> datetime:
    """Timezone-aware now, for defaults set in Python rather than by the DB."""
    return datetime.now(UTC)


def live_unique(name: str, *columns: str) -> Index:
    """A uniqueness rule that binds live rows only.

    Use this, never ``UniqueConstraint(..., "deleted_at")``. The obvious
    spelling of "unique among rows that are not deleted" is to add
    ``deleted_at`` to a composite unique constraint, and it does not work: SQL
    treats two NULLs as distinct, so every live row — all of which have
    ``deleted_at IS NULL`` — is unique against every other live row and the
    constraint fires only between two rows soft deleted at the identical
    microsecond. Written that way, GSTIN uniqueness admitted duplicates and the
    filing generator's idempotency guarantee held for nothing.

    A partial unique index says it properly: unique across the named columns,
    over the subset of rows where ``deleted_at IS NULL``. Postgres and SQLite
    both support it, so the constraint the test suite exercises is the
    constraint production has.

    The predicate is spelled twice because SQLAlchemy takes it per dialect;
    there is no portable spelling. ``Index`` rather than ``UniqueConstraint``
    because only an index can carry a WHERE clause.
    """
    predicate = text("deleted_at IS NULL")
    return Index(
        name,
        *columns,
        unique=True,
        postgresql_where=predicate,
        sqlite_where=predicate,
    )


class TimestampMixin:
    """``created_at`` / ``updated_at``, maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """``deleted_at``, set instead of issuing a DELETE.

    Queries filter on ``deleted_at.is_(None)``; see
    :func:`app.core.tenancy.scoped` which applies both that and the tenant
    filter so a route cannot remember one and forget the other.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def soft_delete(self) -> None:
        self.deleted_at = utcnow()


class OrgScopedMixin:
    """A non-null ``organization_id`` FK plus the indexes tenant reads need.

    Declared here rather than per-model so that "multi-tenant from day one" is
    structural: a table that inherits this cannot be written without a tenant.

    Note that ``ondelete`` is RESTRICT, not CASCADE. Organizations are soft
    deleted; a hard DELETE of one would take its filings and — worse — attempt
    to take its audit trail with it, and the audit trail is append-only by
    contract. Making the database refuse is cheaper than trusting nobody will
    ever run that statement.
    """

    # Supplied by the concrete model; declared so the index names below can be
    # built from it without the type checker losing track of the attribute.
    __tablename__: ClassVar[str]

    @declared_attr
    def organization_id(cls) -> Mapped[int]:  # noqa: N805
        return mapped_column(
            ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
        )

    @declared_attr.directive
    def __table_args__(cls) -> tuple:  # noqa: N805
        return (
            Index(f"ix_{cls.__tablename__}_org_created", "organization_id", "created_at"),
        )
