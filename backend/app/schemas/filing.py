"""Filing, deadline and calendar bodies."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

from app.models.enums import FilingStatus, Frequency, Regulation
from app.schemas.common import ORMModel, Paise


class FilingSummary(ORMModel):
    """A filing as it appears in a list or on the calendar.

    Carries the derived fields — ``effective_due_date``, ``days_until_due``,
    ``urgency`` — because every consumer needs them and each would otherwise
    recompute them from ``due_date`` and ``extended_due_date``. Three clients
    computing the same urgency band is three chances to get the boundary wrong.
    """

    id: int
    organization_id: int
    obligation_id: int
    regulation: Regulation
    filing_type: str | None = None
    period_key: str
    period_start: date | None = None
    period_end: date | None = None
    due_date: date
    extended_due_date: date | None = None
    status: FilingStatus
    is_ai_generated: bool = False
    created_at: datetime

    title: str | None = None
    effective_due_date: date | None = None
    days_until_due: int | None = None
    urgency: str | None = None
    is_open: bool | None = None


class FilingResponse(FilingSummary):
    """One filing in full, including its payload and review trail."""

    data_json: dict | None = None
    template_id: int | None = None

    ai_model: str | None = None
    ai_generated_at: datetime | None = None
    ai_confidence: int | None = None
    ai_notes: str | None = None

    prepared_by_id: int | None = None
    reviewed_by_id: int | None = None
    reviewed_at: datetime | None = None
    submitted_at: datetime | None = None
    submitted_by_id: int | None = None
    acknowledgement_no: str | None = None
    rejection_reason: str | None = None

    tax_payable_paise: int | None = None
    tax_paid_paise: int | None = None
    penalty_paise: int | None = None
    late_fee_paise: int | None = None

    notes: str | None = None
    updated_at: datetime

    # What this filing may become next, so the client renders the right buttons
    # instead of offering every action and discovering the answer via a 409.
    allowed_transitions: list[FilingStatus] = []
    obligation_code: str | None = None
    obligation_title: str | None = None
    penalty_description: str | None = None


class FilingCreateRequest(BaseModel):
    """Create a filing by hand.

    The generator covers the recurring calendar; this is for the cases it
    cannot know about — an event-based return after an allotment, a revised
    return, a period the client was onboarded too late for.
    """

    obligation_id: int
    period_key: str = Field(min_length=1, max_length=32)
    due_date: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    data_json: dict | None = None
    template_id: int | None = None
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _period_is_ordered(self):
        if (
            self.period_start is not None
            and self.period_end is not None
            and self.period_start > self.period_end
        ):
            raise ValueError("period_start falls after period_end")
        return self


class FilingUpdateRequest(BaseModel):
    """Edit a filing's content. Status is moved through its own endpoint.

    Separating the two is what makes the workflow rules enforceable: if status
    were writable here, every guard in
    :mod:`app.services.filing_workflow` could be bypassed by a PATCH.
    """

    data_json: dict | None = None
    template_id: int | None = None
    extended_due_date: date | None = None
    tax_payable_paise: Paise | None = None
    tax_paid_paise: Paise | None = None
    penalty_paise: Paise | None = None
    late_fee_paise: Paise | None = None
    notes: str | None = Field(default=None, max_length=4000)


class FilingTransitionRequest(BaseModel):
    """Move a filing to another status."""

    status: FilingStatus
    acknowledgement_no: str | None = Field(default=None, max_length=128)
    rejection_reason: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=4000)


class FilingTransitionResponse(BaseModel):
    filing: FilingResponse
    previous_status: FilingStatus
    # True when a SUBMITTED was recorded as LATE_FILED because the due date had
    # passed. Surfaced so the UI can say so rather than the user noticing the
    # status is not the one they picked.
    recorded_as_late: bool = False


class DeadlineResponse(ORMModel):
    id: int
    organization_id: int
    filing_id: int
    due_date: date
    reminders_sent_json: list | None = None
    last_reminder_at: datetime | None = None
    escalation_level: int
    is_satisfied: bool
    satisfied_at: datetime | None = None


class CalendarItem(BaseModel):
    """One dated entry on the compliance calendar.

    Not a filing schema, because the calendar shows more than filings: an
    extracted deadline from a parsed show-cause notice belongs on it too, and
    so does a DPDP breach-notification clock. ``source`` says which.
    """

    date: date
    source: str = "filing"
    filing_id: int | None = None
    organization_id: int
    organization_name: str | None = None
    regulation: Regulation | None = None
    filing_type: str | None = None
    title: str
    period_key: str | None = None
    status: FilingStatus | None = None
    days_until_due: int
    urgency: str
    penalty_per_day_paise: int | None = None
    is_open: bool = True


class CalendarResponse(BaseModel):
    """A window of the calendar, with the counts the header strip renders."""

    start: date
    end: date
    items: list[CalendarItem]
    total: int
    overdue: int
    due_this_week: int
    by_urgency: dict[str, int]
    by_regulation: dict[str, int]


class GenerationResponse(BaseModel):
    """What a calendar-generation run produced."""

    organization_id: int
    created: int
    skipped_existing: int
    skipped_conflict: int
    obligations_considered: int


class EventFilingRequest(BaseModel):
    """Record the event that an event-based obligation hangs off.

    FC-GPR is due 30 days after an allotment; the allotment is the input. The
    due date is computed from the obligation's own ``offset_days`` rather than
    accepted from the client, because the offset is the statutory part and a
    caller that could supply it could quietly give itself a later deadline.
    """

    obligation_id: int
    event_date: date
    data_json: dict | None = None
    notes: str | None = Field(default=None, max_length=4000)


class UpcomingFilingsQuery(BaseModel):
    """Shared filters for the filing list and the calendar."""

    regulation: Regulation | None = None
    status: FilingStatus | None = None
    frequency: Frequency | None = None
    due_before: date | None = None
    due_after: date | None = None
    only_open: bool = False
    search: str | None = Field(default=None, max_length=128)
