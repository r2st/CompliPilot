"""The tamper-evident audit trail (section 4.7).

Append-only, by three separate mechanisms, because any one of them alone is a
convention rather than a guarantee:

1. The model has no soft-delete mixin and no mutable columns by contract.
2. :mod:`app.services.audit` is the only writer, and it never issues UPDATE.
3. A migration installs Postgres triggers that raise on UPDATE and DELETE, so
   even a psql session with the application's credentials cannot rewrite
   history.

Each row's ``checksum`` is a SHA-256 over the row's own content *and* the
previous row's checksum, which makes the trail a hash chain: altering entry N
invalidates every entry after it, and the tamper shows up as a break at a known
position rather than as an unfalsifiable claim that nothing changed.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.enums import AuditAction
from app.models.mixins import JSONType, utcnow

# The checksum stored for the first entry of a chain, standing in for "the
# previous entry" that does not exist. A constant rather than NULL so the
# hashing input has the same shape for every row, including the first.
GENESIS_CHECKSUM = "0" * 64


class AuditTrail(Base):
    """One recorded action.

    Note the absence of ``TimestampMixin`` and ``SoftDeleteMixin``. An
    ``updated_at`` on an append-only table is a contradiction, and a
    ``deleted_at`` would be a way to hide an entry — which is exactly what the
    chain exists to prevent.
    """

    __tablename__ = "audit_trails"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Not OrgScopedMixin: that mixin brings a ``created_at`` composite index
    # this table has no ``created_at`` for, and the chain needs its own
    # sequence column anyway.
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # Position in this organization's chain, starting at 1. Chains are
    # per-organization rather than global so that one tenant's write rate does
    # not serialise every other tenant's, and so a tenant can be handed a
    # verifiable export of just their own history.
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Null for actions with no authenticated actor — a failed login with an
    # unknown address, or a scheduled sweep. The system actor is recorded in
    # ``actor_label`` instead, so an entry always says who did it even when
    # there is no user row to point at.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    actor_label: Mapped[str] = mapped_column(String(255), nullable=False, default="system")

    action: Mapped[AuditAction] = mapped_column(
        Enum(AuditAction, native_enum=False, length=32), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # String, not integer: most entities have integer ids but a login has no
    # entity id at all and a bulk export's "id" is a filter description.
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True)

    # Set in Python at construction rather than by the database. The checksum
    # covers this value, so it has to be known before the hash is computed —
    # a ``server_default`` would be assigned after, and every verification
    # would fail.
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )

    # Section 4.7: "who did what, when, from which IP, and the before/after
    # state of modified records".
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)

    before_json: Mapped[dict | None] = mapped_column(JSONType)
    after_json: Mapped[dict | None] = mapped_column(JSONType)
    summary: Mapped[str | None] = mapped_column(Text)

    # The chain. ``prev_checksum`` is the previous entry's ``checksum`` for
    # this organization, or GENESIS_CHECKSUM for the first.
    prev_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    __table_args__ = (
        # Two entries claiming the same position would fork the chain and make
        # "verify from the start" ambiguous. The database refuses; the writer
        # retries with the next sequence.
        UniqueConstraint("organization_id", "sequence", name="uq_audit_org_sequence"),
        # Verification and the audit-trail screen both walk a tenant's chain in
        # order, and this index is what makes that a range scan.
        Index("ix_audit_org_sequence", "organization_id", "sequence"),
        Index("ix_audit_org_timestamp", "organization_id", "timestamp"),
        Index("ix_audit_org_entity", "organization_id", "entity_type", "entity_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<AuditTrail org={self.organization_id} seq={self.sequence} "
            f"{self.action} {self.entity_type}:{self.entity_id}>"
        )
