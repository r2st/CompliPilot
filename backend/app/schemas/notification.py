"""Alert, notification and channel-preference bodies."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import ImpactLevel, NotificationChannel, NotificationStatus, Regulation
from app.schemas.common import ORMModel, validate_phone


class NotificationResponse(ORMModel):
    id: int
    organization_id: int
    channel: NotificationChannel
    recipient: str
    user_id: int | None = None
    subject: str | None = None
    content: str
    status: NotificationStatus
    sent_at: datetime | None = None
    error: str | None = None
    attempts: int
    kind: str
    entity_type: str | None = None
    entity_id: str | None = None
    reminder_offset_days: int | None = None
    created_at: datetime


class NotificationPreferenceResponse(ORMModel):
    id: int
    organization_id: int
    user_id: int | None = None
    email_enabled: bool
    whatsapp_enabled: bool
    sms_enabled: bool
    in_app_enabled: bool
    email_address: str | None = None
    whatsapp_number: str | None = None
    sms_number: str | None = None
    reminder_offsets_json: list | None = None
    quiet_hours_start: int | None = None
    quiet_hours_end: int | None = None
    updated_at: datetime


class NotificationPreferenceUpdate(BaseModel):
    """Set how an organization, or one person in it, wants to be reached."""

    email_enabled: bool | None = None
    whatsapp_enabled: bool | None = None
    sms_enabled: bool | None = None
    in_app_enabled: bool | None = None

    email_address: str | None = Field(default=None, max_length=255)
    whatsapp_number: str | None = None
    sms_number: str | None = None

    # Overrides the deployment's reminder schedule for this tenant. Bounded at
    # both ends: a zero-day reminder is the deadline itself, and anything past
    # a year is a typo rather than a preference.
    reminder_offsets_json: list[int] | None = None
    quiet_hours_start: int | None = Field(default=None, ge=0, le=23)
    quiet_hours_end: int | None = Field(default=None, ge=0, le=23)

    _v_whatsapp = field_validator("whatsapp_number")(validate_phone)
    _v_sms = field_validator("sms_number")(validate_phone)

    @field_validator("reminder_offsets_json")
    @classmethod
    def _sane_offsets(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(v < 0 or v > 365 for v in value):
            raise ValueError("Reminder offsets must be between 0 and 365 days")
        # Descending and de-duplicated, matching what
        # :attr:`app.core.config.Settings.reminder_offsets` guarantees. The
        # sweep walks them furthest-first and stops at the first already sent,
        # which is only correct on a sorted list — so the order is normalised
        # at the boundary rather than trusted from the client.
        return sorted(set(value), reverse=True)

    @model_validator(mode="after")
    def _quiet_hours_are_paired(self):
        if (self.quiet_hours_start is None) != (self.quiet_hours_end is None):
            raise ValueError("Set both quiet_hours_start and quiet_hours_end, or neither")
        return self


class RegulatoryUpdateResponse(ORMModel):
    """A published regulatory change."""

    id: int
    source: str
    source_url: str | None = None
    reference_no: str | None = None
    published_date: date
    effective_date: date | None = None
    title: str
    summary: str | None = None
    regulation: Regulation | None = None
    impact_level: ImpactLevel
    action_required: str | None = None
    compliance_deadline: date | None = None
    is_analysed: bool
    is_published: bool
    created_at: datetime


class RegulatoryImpactResponse(ORMModel):
    """What one regulatory change means for one organization."""

    id: int
    organization_id: int
    update_id: int
    impact_level: ImpactLevel
    rationale: str | None = None
    action_required: str | None = None
    action_deadline: date | None = None
    is_acknowledged: bool
    acknowledged_at: datetime | None = None
    acknowledged_by_id: int | None = None
    created_at: datetime

    update: RegulatoryUpdateResponse | None = None


class AcknowledgeRequest(BaseModel):
    """Record that a human has seen and accepted a regulatory impact."""

    notes: str | None = Field(default=None, max_length=4000)


class AlertSummary(BaseModel):
    """The alert bell's payload: what needs attention, and how badly."""

    unacknowledged_impacts: int
    critical_impacts: int
    overdue_filings: int
    due_within_7_days: int
    open_breach_incidents: int
    overdue_data_requests: int
    failed_notifications: int
