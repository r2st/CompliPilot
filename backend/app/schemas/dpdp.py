"""DPDP Act toolkit bodies: consent, data map, breaches, requests, assessments.

The data principal's identifier — an email, a phone number, a customer ref —
is the most sensitive field in this product. It is stored encrypted with a keyed
fingerprint beside it, exactly as the statutory identifiers are, so:

* requests carry ``principal_ref`` in clear, because that is what the caller
  has and what must be fingerprinted to find prior records;
* responses carry it back only on a single-record read, never in a list. A
  breach register that decrypted 40,000 email addresses to render a page would
  itself be the reportable incident.
"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import ORMModel

# The vocabulary the Act uses, constrained here rather than left free-text so
# the register can be grouped and the DPB export has stable categories.
_LEGAL_BASES = r"^(consent|legitimate_use|legal_obligation|contract|vital_interest)$"
_DSR_TYPES = r"^(access|correction|erasure|nomination|grievance|withdraw_consent)$"
_SEVERITIES = r"^(low|medium|high|critical)$"


class ConsentRecordResponse(ORMModel):
    id: int
    organization_id: int
    principal_type: str
    purpose: str
    purpose_description: str | None = None
    data_categories_json: list | None = None
    notice_version: str
    notice_language: str
    is_granted: bool
    granted_at: datetime | None = None
    withdrawn_at: datetime | None = None
    expires_at: datetime | None = None
    collection_method: str | None = None
    created_at: datetime

    # Only populated on a single-record read. See the module docstring.
    principal_ref: str | None = None


class ConsentCreateRequest(BaseModel):
    """Record one data principal's consent to one purpose."""

    principal_ref: str = Field(min_length=1, max_length=255)
    principal_type: str = Field(default="customer", max_length=32)
    purpose: str = Field(min_length=2, max_length=255)
    purpose_description: str | None = None
    data_categories_json: list[str] | None = None
    notice_version: str = Field(default="1", max_length=32)
    notice_text: str | None = None
    notice_language: str = Field(default="en", max_length=16)
    granted_at: datetime | None = None
    expires_at: datetime | None = None
    collection_method: str | None = Field(default=None, max_length=32)
    evidence_json: dict | None = None


class ConsentWithdrawRequest(BaseModel):
    """Withdraw a consent.

    A distinct endpoint rather than a PATCH setting ``is_granted=false``,
    because withdrawal under the Act is an event with a time and a reason, not
    a field edit — and because the row must remain as evidence of what was
    consented to before it was withdrawn.
    """

    reason: str | None = Field(default=None, max_length=2000)


class DataMapEntryResponse(ORMModel):
    id: int
    organization_id: int
    system_name: str
    system_type: str | None = None
    owner_team: str | None = None
    data_category: str
    data_fields_json: list | None = None
    is_sensitive: bool
    purpose: str
    legal_basis: str
    retention_period_days: int | None = None
    is_transferred_abroad: bool
    transfer_countries_json: list | None = None
    processors_json: list | None = None
    estimated_record_count: int | None = None
    last_reviewed_at: datetime | None = None
    notes: str | None = None
    created_at: datetime


class DataMapEntryRequest(BaseModel):
    """One row of the personal-data inventory."""

    system_name: str = Field(min_length=1, max_length=255)
    system_type: str | None = Field(default=None, max_length=64)
    owner_team: str | None = Field(default=None, max_length=128)
    data_category: str = Field(min_length=1, max_length=64)
    data_fields_json: list[str] | None = None
    is_sensitive: bool = False
    purpose: str = Field(min_length=2, max_length=255)
    legal_basis: str = Field(default="consent", pattern=_LEGAL_BASES)
    retention_period_days: int | None = Field(default=None, ge=0, le=36500)
    is_transferred_abroad: bool = False
    transfer_countries_json: list[str] | None = None
    processors_json: list[str] | None = None
    estimated_record_count: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _transfers_name_a_country(self):
        # A cross-border transfer with no destination cannot be assessed
        # against the Act's restricted-territory rules, which is the only
        # reason the flag exists.
        if self.is_transferred_abroad and not self.transfer_countries_json:
            raise ValueError(
                "A cross-border transfer must name at least one destination country"
            )
        return self


class BreachIncidentResponse(ORMModel):
    id: int
    organization_id: int
    reference: str
    title: str
    description: str | None = None
    severity: str
    status: str
    occurred_at: datetime | None = None
    detected_at: datetime
    contained_at: datetime | None = None
    affected_principals_count: int | None = None
    affected_data_categories_json: list | None = None
    affected_systems_json: list | None = None
    dpb_notified_at: datetime | None = None
    dpb_reference: str | None = None
    principals_notified_at: datetime | None = None
    root_cause: str | None = None
    remediation: str | None = None
    closed_at: datetime | None = None
    reported_by_id: int | None = None
    created_at: datetime

    # The 72-hour clock, computed by the router. Negative once it has run out.
    hours_until_dpb_deadline: float | None = None
    dpb_notification_overdue: bool | None = None


class BreachCreateRequest(BaseModel):
    """Open a breach incident and start the 72-hour clock.

    ``detected_at`` is what the clock runs from and defaults to now. It is
    settable because a breach is often entered hours after it was found, and
    backdating it to the truth is what makes the deadline correct — the
    alternative is a register that quietly understates every response time.
    """

    title: str = Field(min_length=2, max_length=512)
    description: str | None = None
    severity: str = Field(default="medium", pattern=_SEVERITIES)
    occurred_at: datetime | None = None
    detected_at: datetime | None = None
    affected_principals_count: int | None = Field(default=None, ge=0)
    affected_data_categories_json: list[str] | None = None
    affected_systems_json: list[str] | None = None


class BreachUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=512)
    description: str | None = None
    severity: str | None = Field(default=None, pattern=_SEVERITIES)
    status: str | None = Field(
        default=None, pattern=r"^(open|contained|notified|closed)$"
    )
    contained_at: datetime | None = None
    affected_principals_count: int | None = Field(default=None, ge=0)
    affected_data_categories_json: list[str] | None = None
    affected_systems_json: list[str] | None = None
    root_cause: str | None = None
    remediation: str | None = None


class BreachNotifyRequest(BaseModel):
    """Record that the Data Protection Board, or the principals, were told."""

    dpb_reference: str | None = Field(default=None, max_length=128)
    notification_text: str | None = None
    notify_principals: bool = False


class DataSubjectRequestResponse(ORMModel):
    id: int
    organization_id: int
    reference: str
    request_type: str
    principal_name: str | None = None
    details: str | None = None
    status: str
    received_at: datetime
    due_date: date
    verified_at: datetime | None = None
    completed_at: datetime | None = None
    response: str | None = None
    rejection_reason: str | None = None
    assigned_to_id: int | None = None
    created_at: datetime

    days_until_due: int | None = None
    is_overdue: bool | None = None
    principal_ref: str | None = None


class DataSubjectRequestCreate(BaseModel):
    """Log a request from a data principal. The SLA clock starts at receipt."""

    request_type: str = Field(pattern=_DSR_TYPES)
    principal_ref: str = Field(min_length=1, max_length=255)
    principal_name: str | None = Field(default=None, max_length=255)
    details: str | None = Field(default=None, max_length=8000)
    received_at: datetime | None = None
    assigned_to_id: int | None = None


class DataSubjectRequestUpdate(BaseModel):
    status: str | None = Field(
        default=None, pattern=r"^(received|verifying|in_progress|completed|rejected)$"
    )
    details: str | None = Field(default=None, max_length=8000)
    response: str | None = Field(default=None, max_length=16000)
    rejection_reason: str | None = Field(default=None, max_length=2000)
    assigned_to_id: int | None = None
    mark_verified: bool = False

    @model_validator(mode="after")
    def _rejection_has_a_reason(self):
        if self.status == "rejected" and not self.rejection_reason:
            raise ValueError("Rejecting a data principal's request requires a reason")
        return self


class PIAResponse(ORMModel):
    id: int
    organization_id: int
    title: str
    processing_activity: str
    status: str
    answers_json: dict | None = None
    template_id: int | None = None
    risk_score: int | None = None
    risk_level: str | None = None
    risk_rationale: str | None = None
    ai_model: str | None = None
    mitigations_json: list | None = None
    residual_risk: str | None = None
    reviewed_by_id: int | None = None
    reviewed_at: datetime | None = None
    next_review_date: date | None = None
    created_at: datetime


class PIARequest(BaseModel):
    """A privacy impact assessment, drafted or updated."""

    title: str = Field(min_length=2, max_length=512)
    processing_activity: str = Field(min_length=2)
    answers_json: dict | None = None
    template_id: int | None = None
    mitigations_json: list[str] | None = None
    residual_risk: str | None = Field(default=None, pattern=_SEVERITIES)
    next_review_date: date | None = None


class DPDPReadinessResponse(BaseModel):
    """A single score for "how ready are we if the DPB asks".

    Deliberately a small number of coarse signals rather than a precise
    percentage. The Act's Rules are not fully notified; a score that implied
    two-decimal accuracy about compliance with an unfinished regulation would
    be false confidence, and this product's job is not to sell that.
    """

    has_data_map: bool
    data_map_entries: int
    consent_records: int
    active_consents: int
    withdrawn_consents: int
    open_breaches: int
    overdue_dpb_notifications: int
    open_data_requests: int
    overdue_data_requests: int
    completed_assessments: int
    cross_border_transfers: int
    score: int = Field(ge=0, le=100)
    gaps: list[str] = []
