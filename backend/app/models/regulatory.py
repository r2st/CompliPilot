"""Regulatory updates and their per-client impact assessments (sections 2.2, 4.6)."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import ImpactLevel, Regulation
from app.models.mixins import (
    JSONType,
    OrgScopedMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    live_unique,
)


class RegulatoryUpdate(Base, TimestampMixin, SoftDeleteMixin):
    """A circular, notification or amendment picked up by the pipeline.

    System-wide, not tenant-owned: the CBIC issues one circular, and every
    tenant sees the same one. What differs per tenant is the
    :class:`RegulatoryImpact` derived from it, which is where
    ``organization_id`` appears.
    """

    __tablename__ = "regulatory_updates"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Where it came from: "cbic", "mca", "rbi", "sebi", "egazette".
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_url: Mapped[str | None] = mapped_column(String(1024))
    # The regulator's own reference — "Circular No. 210/4/2024-GST". Part of
    # the uniqueness key, so re-scraping the same page does not duplicate.
    reference_no: Mapped[str | None] = mapped_column(String(255), index=True)

    published_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    effective_date: Mapped[date | None] = mapped_column(Date)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    # The extracted body. Large; excluded from list responses by the schema so
    # a 200-row alert page does not ship a megabyte of circular text.
    content: Mapped[str | None] = mapped_column(Text)

    regulation: Mapped[Regulation | None] = mapped_column(
        Enum(Regulation, native_enum=False, length=32), index=True
    )
    # A circular can touch more than one domain — an FDI rule change is FEMA
    # and RBI and sometimes MCA — so the single ``regulation`` above is the
    # primary one and this is the full set.
    domains_json: Mapped[list | None] = mapped_column(JSONType)

    impact_level: Mapped[ImpactLevel] = mapped_column(
        Enum(ImpactLevel, native_enum=False, length=32),
        default=ImpactLevel.MEDIUM,
        nullable=False,
        index=True,
    )

    # What the LLM analyser extracted (pipeline step 3): affected entity
    # types, states, turnover bands, the obligations touched, and the actions
    # required. Kept as JSON because the shape genuinely varies by domain.
    analysis_json: Mapped[dict | None] = mapped_column(JSONType)
    affected_entity_types_json: Mapped[list | None] = mapped_column(JSONType)
    affected_states_json: Mapped[list | None] = mapped_column(JSONType)
    affected_obligation_codes_json: Mapped[list | None] = mapped_column(JSONType)
    action_required: Mapped[str | None] = mapped_column(Text)
    compliance_deadline: Mapped[date | None] = mapped_column(Date)

    # Whether the impact mapper has already fanned this out to tenants. The
    # sweep picks up rows where this is false, so a mapper crash resumes
    # rather than skipping.
    is_analysed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    analysed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    metadata_json: Mapped[dict | None] = mapped_column(JSONType)

    __table_args__ = (
        # Re-scraping is expected and must be idempotent. Source plus the
        # regulator's reference identifies the document; where a source
        # publishes no reference number the title stands in, which is why the
        # constraint carries all three.
        live_unique("uq_reg_updates_source_ref", "source", "reference_no", "title"),
        Index("ix_reg_updates_published", "published_date", "impact_level"),
        Index("ix_reg_updates_pending", "is_analysed", "published_date"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RegulatoryUpdate {self.source} {self.reference_no} {self.impact_level}>"


class RegulatoryImpact(Base, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    """What one regulatory update means for one organization.

    Section 4.6: "what changed, who is affected, what action is required, and
    the deadline for compliance". The first of those lives on the update; the
    other three are per-tenant and live here.
    """

    __tablename__ = "regulatory_impacts"

    id: Mapped[int] = mapped_column(primary_key=True)

    update_id: Mapped[int] = mapped_column(
        ForeignKey("regulatory_updates.id", ondelete="CASCADE"), nullable=False, index=True
    )

    impact_level: Mapped[ImpactLevel] = mapped_column(
        Enum(ImpactLevel, native_enum=False, length=32), nullable=False, index=True
    )
    # Why the mapper concluded what it did — "entity_type=private_limited
    # matches; turnover ₹8.2 Cr is above the ₹5 Cr threshold". Shown verbatim,
    # because a compliance officer will not act on a level they cannot check.
    rationale: Mapped[str | None] = mapped_column(Text)
    action_required: Mapped[str | None] = mapped_column(Text)
    action_deadline: Mapped[date | None] = mapped_column(Date)

    # Obligations of this organization that the update touches, so the UI can
    # link straight from the alert to the affected calendar entries.
    affected_obligation_ids_json: Mapped[list | None] = mapped_column(JSONType)

    # Acknowledgement, so an alert can be cleared without being deleted. The
    # audit trail records who cleared it and when.
    is_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acknowledged_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    is_notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    update: Mapped[RegulatoryUpdate] = relationship()

    __table_args__ = (
        # One assessment per (org, update). Re-running the mapper updates the
        # existing row rather than stacking duplicates in the alert list.
        live_unique("uq_impact_org_update", "organization_id", "update_id"),
        Index("ix_impacts_org_level", "organization_id", "impact_level"),
        Index("ix_impacts_org_open", "organization_id", "is_acknowledged"),
        Index("ix_regulatory_impacts_org_created", "organization_id", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RegulatoryImpact org={self.organization_id} update={self.update_id}>"
