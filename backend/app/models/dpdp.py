"""DPDP Act toolkit tables (section 4.4).

Five capabilities, five tables: consent records, the personal-data map, breach
notifications, data subject requests, and privacy impact assessments. They live
together because they share the Act's vocabulary — *data principal* for the
individual, *data fiduciary* for the organization — and because a DPB inquiry
asks about all five at once.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import (
    JSONType,
    OrgScopedMixin,
    SoftDeleteMixin,
    TimestampMixin,
)

# Section 8(6) of the DPDP Act: the Data Protection Board must be told of a
# breach without delay. The product treats 72 hours as the operative clock —
# the number the design document commits to and the one every SLA below is
# measured against.
BREACH_NOTIFICATION_HOURS = 72

# The Act gives no single statutory turnaround for a data principal request;
# 30 days is the market norm and what the SLA timer uses until the Rules fix a
# number. Declared here rather than inline so changing it is one edit.
DSR_SLA_DAYS = 30


class ConsentRecord(Base, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    """One data principal's consent, at one version of one notice.

    Consent under the Act is per-purpose and revocable, and the fiduciary must
    be able to *prove* what was consented to. That is why withdrawal is a new
    state on the row rather than a delete, and why the notice version is
    recorded: showing "they agreed" is worthless without showing to what.
    """

    __tablename__ = "consent_records"

    id: Mapped[int] = mapped_column(primary_key=True)

    # The individual. Stored as an encrypted identifier plus a keyed
    # fingerprint, so consent can be looked up by email or phone without the
    # column holding readable contact details for every customer of the tenant.
    principal_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    principal_type: Mapped[str] = mapped_column(String(32), nullable=False, default="customer")

    purpose: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    purpose_description: Mapped[str | None] = mapped_column(Text)
    # Which categories the consent covers — ["contact", "financial"]. Joins to
    # the data map, so a withdrawal can name the systems that must forget.
    data_categories_json: Mapped[list | None] = mapped_column(JSONType)

    # The notice as served, and its version. Both are the evidence.
    notice_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    notice_text: Mapped[str | None] = mapped_column(Text)
    # The Act requires the notice be available in the Eighth Schedule
    # languages; recording which one was actually served is part of proving
    # the consent was informed.
    notice_language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")

    is_granted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # How it was collected: "web_form", "app", "paper", "api". Plus the
    # evidence trail — IP and the request id at the moment of collection.
    collection_method: Mapped[str | None] = mapped_column(String(32))
    source_ip: Mapped[str | None] = mapped_column(String(45))
    evidence_json: Mapped[dict | None] = mapped_column(JSONType)

    __table_args__ = (
        # A principal can hold at most one live consent per purpose per notice
        # version. A new version is a new row, which is what preserves the
        # history the Act asks the fiduciary to be able to produce.
        UniqueConstraint(
            "organization_id",
            "principal_fingerprint",
            "purpose",
            "notice_version",
            "deleted_at",
            name="uq_consent_principal_purpose_version",
        ),
        Index("ix_consent_org_principal", "organization_id", "principal_fingerprint"),
        Index("ix_consent_org_active", "organization_id", "is_granted"),
        Index("ix_consent_records_org_created", "organization_id", "created_at"),
    )

    @property
    def is_active(self) -> bool:
        """Whether the consent presently authorises processing."""
        if not self.is_granted or self.withdrawn_at is not None:
            return False
        if self.expires_at is not None:
            from app.models.mixins import utcnow

            return self.expires_at > utcnow()
        return True

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ConsentRecord org={self.organization_id} purpose={self.purpose!r}>"


class DataMapEntry(Base, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    """One place personal data lives, and why.

    The record of processing activities in all but name. It is what makes a
    breach assessment and an erasure request answerable: without knowing which
    systems hold a principal's data, neither question has an answer.
    """

    __tablename__ = "data_map_entries"

    id: Mapped[int] = mapped_column(primary_key=True)

    system_name: Mapped[str] = mapped_column(String(255), nullable=False)
    system_type: Mapped[str | None] = mapped_column(String(64))
    owner_team: Mapped[str | None] = mapped_column(String(128))

    data_category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    data_fields_json: Mapped[list | None] = mapped_column(JSONType)
    # The Act does not define a "sensitive" tier the way the GDPR does, but
    # financial and biometric data drive a different breach severity, so the
    # flag earns its column.
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    purpose: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_basis: Mapped[str] = mapped_column(String(64), nullable=False, default="consent")
    retention_period_days: Mapped[int | None] = mapped_column()

    # Cross-border transfer, which the Act restricts by notified country.
    is_transferred_abroad: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    transfer_countries_json: Mapped[list | None] = mapped_column(JSONType)
    processors_json: Mapped[list | None] = mapped_column(JSONType)

    estimated_record_count: Mapped[int | None] = mapped_column()
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "system_name",
            "data_category",
            "deleted_at",
            name="uq_datamap_system_category",
        ),
        Index("ix_datamap_org_sensitive", "organization_id", "is_sensitive"),
        Index("ix_data_map_entries_org_created", "organization_id", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DataMapEntry {self.system_name}/{self.data_category}>"


class BreachIncident(Base, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    """A personal data breach, from detection to closure.

    The 72-hour clock runs from ``detected_at``, not from ``occurred_at``: the
    fiduciary cannot report what it does not know about, and back-dating the
    clock to the intrusion would make every late discovery an automatic
    violation. Both are recorded because the gap between them is itself a
    finding.
    """

    __tablename__ = "breach_incidents"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Human-facing case number, unique per tenant — "BR-2026-0004".
    reference: Mapped[str] = mapped_column(String(32), nullable=False)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="detected", index=True
    )

    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    contained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    affected_principals_count: Mapped[int | None] = mapped_column()
    affected_data_categories_json: Mapped[list | None] = mapped_column(JSONType)
    affected_systems_json: Mapped[list | None] = mapped_column(JSONType)

    # --- Notification obligations ----------------------------------------
    dpb_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dpb_reference: Mapped[str | None] = mapped_column(String(128))
    dpb_notification_text: Mapped[str | None] = mapped_column(Text)
    principals_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    principals_notification_text: Mapped[str | None] = mapped_column(Text)

    root_cause: Mapped[str | None] = mapped_column(Text)
    remediation: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    reported_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSONType)

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "reference", "deleted_at", name="uq_breach_org_reference"
        ),
        Index("ix_breach_org_status", "organization_id", "status"),
        Index("ix_breach_org_detected", "organization_id", "detected_at"),
        Index("ix_breach_incidents_org_created", "organization_id", "created_at"),
    )

    @property
    def dpb_deadline(self) -> datetime:
        """When the Board must have been told by."""
        from datetime import timedelta

        return self.detected_at + timedelta(hours=BREACH_NOTIFICATION_HOURS)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<BreachIncident {self.reference} {self.status}>"


class DataSubjectRequest(Base, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    """An access, correction, erasure or grievance request, with its SLA clock."""

    __tablename__ = "data_subject_requests"

    id: Mapped[int] = mapped_column(primary_key=True)

    reference: Mapped[str] = mapped_column(String(32), nullable=False)
    # "access", "correction", "erasure", "nomination", "grievance".
    request_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    principal_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    principal_name: Mapped[str | None] = mapped_column(String(255))

    details: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="received", index=True
    )

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # An unverified requester must not be handed someone else's data, so
    # verification is a distinct step with its own timestamp rather than an
    # assumption baked into ``received_at``.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    response: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    assigned_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSONType)

    __table_args__ = (
        UniqueConstraint("organization_id", "reference", "deleted_at", name="uq_dsr_org_ref"),
        Index("ix_dsr_org_status_due", "organization_id", "status", "due_date"),
        Index("ix_data_subject_requests_org_created", "organization_id", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DataSubjectRequest {self.reference} {self.request_type} {self.status}>"


class PrivacyImpactAssessment(Base, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    """A guided PIA and its risk score."""

    __tablename__ = "privacy_impact_assessments"

    id: Mapped[int] = mapped_column(primary_key=True)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    processing_activity: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)

    # The questionnaire, as answered. Its shape comes from the PIA template in
    # the library, so it versions with the template rather than with a
    # migration.
    answers_json: Mapped[dict | None] = mapped_column(JSONType)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("templates.id", ondelete="SET NULL")
    )

    # 0-100, AI-assisted. Stored with the model that produced it, because a
    # score whose provenance is unknown cannot be defended.
    risk_score: Mapped[int | None] = mapped_column()
    risk_level: Mapped[str | None] = mapped_column(String(16))
    risk_rationale: Mapped[str | None] = mapped_column(Text)
    ai_model: Mapped[str | None] = mapped_column(String(128))

    mitigations_json: Mapped[list | None] = mapped_column(JSONType)
    residual_risk: Mapped[str | None] = mapped_column(String(16))

    reviewed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_review_date: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        Index("ix_pia_org_status", "organization_id", "status"),
        Index("ix_privacy_impact_assessments_org_created", "organization_id", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PrivacyImpactAssessment id={self.id} {self.status} risk={self.risk_score}>"
