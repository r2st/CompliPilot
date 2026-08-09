"""Filings — one instance of an obligation for one period, and its deadline row."""
from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

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
from app.models.enums import FilingStatus, Regulation
from app.models.mixins import (
    JSONType,
    OrgScopedMixin,
    Paise,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    live_unique,
)

if TYPE_CHECKING:
    from app.models.obligation import ComplianceObligation


class Filing(Base, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    """One filing instance: "GSTR-3B for 2026-07, for org 42".

    Created by the calendar generator ahead of the period, by the event
    recorder for event-based obligations, or by hand. It carries the draft
    payload, the review trail, and — once filed — the portal's acknowledgement.
    """

    __tablename__ = "filings"

    id: Mapped[int] = mapped_column(primary_key=True)

    obligation_id: Mapped[int] = mapped_column(
        ForeignKey("compliance_obligations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Denormalised from the obligation. Every dashboard filters by regulation,
    # and doing it through a join means the index cannot cover the query.
    regulation: Mapped[Regulation] = mapped_column(
        Enum(Regulation, native_enum=False, length=32), nullable=False, index=True
    )
    filing_type: Mapped[str | None] = mapped_column(String(64), index=True)

    # --- Period ----------------------------------------------------------
    # ``period_key`` is the human and machine handle for the period covered:
    # "2026-07" for a month, "2026-Q2" for a quarter, "FY2026-27" for a year,
    # and for an event-based filing the ISO date of the event. It is part of
    # the uniqueness constraint, so it is what stops the generator creating the
    # same return twice.
    period_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)

    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Set when a regulator extends a deadline, which happens often enough with
    # GST that overwriting ``due_date`` would destroy the evidence of what the
    # original date was. The effective date is ``extended_due_date or due_date``.
    extended_due_date: Mapped[date | None] = mapped_column(Date)

    status: Mapped[FilingStatus] = mapped_column(
        Enum(FilingStatus, native_enum=False, length=32),
        default=FilingStatus.NOT_STARTED,
        nullable=False,
        index=True,
    )

    # --- Content ---------------------------------------------------------
    # The filing's field values, shaped by the template for its type. JSON
    # rather than a table per form: there are ~40 forms across eight
    # regulations and their fields change with every amendment.
    data_json: Mapped[dict | None] = mapped_column(JSONType)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("templates.id", ondelete="SET NULL"), index=True
    )

    # Set when section 4.2's generator produced the draft. Kept as three
    # columns because "was this written by a model, which one, and did a human
    # look at it afterwards" is the first question at an audit.
    is_ai_generated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_model: Mapped[str | None] = mapped_column(String(128))
    ai_generated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ai_confidence: Mapped[int | None] = mapped_column()
    ai_notes: Mapped[str | None] = mapped_column(Text)

    # --- Review and submission -------------------------------------------
    prepared_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    reviewed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    submitted_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    acknowledgement_no: Mapped[str | None] = mapped_column(String(128), index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    # --- Money -----------------------------------------------------------
    # All paise. ``tax_payable`` is what the return computes; ``penalty`` is
    # what lateness cost. Both are nullable because most forms carry no
    # amount — MGT-7 does not.
    tax_payable_paise: Mapped[int | None] = mapped_column(Paise)
    tax_paid_paise: Mapped[int | None] = mapped_column(Paise)
    penalty_paise: Mapped[int | None] = mapped_column(Paise)
    late_fee_paise: Mapped[int | None] = mapped_column(Paise)

    notes: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSONType)

    obligation: Mapped["ComplianceObligation"] = relationship()  # noqa: F821,UP037

    __table_args__ = (
        # The generator is idempotent because of this: running the sweep twice
        # for July cannot produce two GSTR-3Bs. Scoped on ``deleted_at`` so a
        # filing voided in error can be recreated.
        live_unique(
            "uq_filings_org_obligation_period",
            "organization_id",
            "obligation_id",
            "period_key",
        ),
        # The calendar's primary query: this org, ordered by due date, often
        # filtered by status.
        Index("ix_filings_org_due", "organization_id", "due_date"),
        Index("ix_filings_org_status_due", "organization_id", "status", "due_date"),
        Index("ix_filings_org_created", "organization_id", "created_at"),
    )

    @property
    def effective_due_date(self) -> date:
        """The date that actually binds — the extension if one was granted."""
        return self.extended_due_date or self.due_date

    @property
    def is_open(self) -> bool:
        """Whether this filing still needs work.

        ``NOT_APPLICABLE`` counts as closed: someone decided it is not owed,
        and it should stop appearing in the "outstanding" count.
        """
        return self.status not in (
            FilingStatus.SUBMITTED,
            FilingStatus.ACKNOWLEDGED,
            FilingStatus.LATE_FILED,
            FilingStatus.NOT_APPLICABLE,
        )

    def days_until_due(self, today: date | None = None) -> int:
        """Days from *today* to the effective due date; negative once overdue."""
        reference = today or date.today()
        return (self.effective_due_date - reference).days

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Filing id={self.id} {self.filing_type} {self.period_key} {self.status}>"


class Deadline(Base, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    """The reminder state for one filing.

    Split from ``filings`` rather than folded into it because the two are
    written by different things at different rates: a filing is edited by
    people, a deadline is swept by the scheduler every hour. Keeping the
    high-churn reminder bookkeeping out of the filing row means the reminder
    sweep never contends with someone editing a draft.
    """

    __tablename__ = "deadlines"

    id: Mapped[int] = mapped_column(primary_key=True)

    filing_id: Mapped[int] = mapped_column(
        ForeignKey("filings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # The reminder offsets already sent, e.g. ``[30, 15]``. A list rather than
    # a counter so that a scheduler outage which skips the 15-day reminder does
    # not also suppress the 7-day one, and so the sweep is idempotent: it sends
    # an offset only if the offset is absent from here.
    reminders_sent_json: Mapped[list | None] = mapped_column(JSONType)
    last_reminder_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # 0 = normal reminders. Raised once the date passes and the filing is still
    # open, which escalates who gets told: staff, then the compliance manager,
    # then the org admin.
    escalation_level: Mapped[int] = mapped_column(default=0, nullable=False)

    # Cleared when the filing is submitted, so the sweep can skip it with an
    # index-only scan rather than joining to check the filing's status.
    is_satisfied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    satisfied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    filing: Mapped[Filing] = relationship()

    __table_args__ = (
        # One deadline row per filing. The sweep would otherwise send a
        # duplicate reminder for every extra row.
        live_unique("uq_deadlines_filing", "filing_id"),
        # The sweep's query: unsatisfied, due within the window. Leading with
        # ``is_satisfied`` keeps the scan off the (large, growing) history of
        # closed deadlines.
        Index("ix_deadlines_sweep", "is_satisfied", "due_date"),
        Index("ix_deadlines_org_due", "organization_id", "due_date"),
        Index("ix_deadlines_org_created", "organization_id", "created_at"),
    )

    @property
    def reminders_sent(self) -> list[int]:
        return list(self.reminders_sent_json or [])

    def has_sent(self, offset_days: int) -> bool:
        return offset_days in self.reminders_sent

    def record_sent(self, offset_days: int) -> None:
        """Mark one offset as sent.

        Reassigns the list rather than appending in place: SQLAlchemy does not
        track mutation of a plain JSON list, so an ``append`` would be lost on
        flush and the same reminder would go out on every sweep.
        """
        if not self.has_sent(offset_days):
            self.reminders_sent_json = [*self.reminders_sent, offset_days]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Deadline filing={self.filing_id} due={self.due_date} lvl={self.escalation_level}>"
