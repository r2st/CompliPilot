"""Period arithmetic and due-date calculation.

This is the module the regulatory test suite of section 9.2 exercises hardest,
because everything else in the product is downstream of it: a wrong due date
produces a wrong reminder, a wrong calendar colour, and eventually a real
penalty for a real business.

Three ideas carry the whole file:

* A **period** is what is being reported on — July 2026, Q2 FY2026-27, FY2025-26.
  :func:`periods_between` enumerates them for a frequency.
* A **due date** is when the return for that period must be filed. It is the
  period's end plus the obligation's rule, and it is almost never in the same
  month as the period.
* The **Indian financial year** runs April to March, and is the frame for every
  annual and quarterly obligation here. FY2026-27 begins 1 April 2026.

Everything takes an explicit ``today`` so tests are not at the mercy of the
clock, and returns :class:`datetime.date` rather than datetime — statutory
deadlines are dates, and attaching a time to one invites a timezone bug that
moves a filing to the wrong day.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from app.models.enums import Frequency

# The Indian financial year starts in April.
FY_START_MONTH = 4

# Urgency bands from section 4.1, as (label, inclusive upper bound in days).
# Ordered nearest-first because :func:`urgency` returns the first match.
URGENCY_BANDS: tuple[tuple[str, int], ...] = (
    ("overdue", -1),
    ("red", 7),
    ("orange", 15),
    ("yellow", 30),
)
URGENCY_DEFAULT = "green"


def urgency(days_until_due: int) -> str:
    """The colour band for a deadline that is *days_until_due* away.

    Section 4.1: green > 30 days, yellow 15-30, orange 7-15, red < 7. The
    boundaries are read as "up to and including", so a deadline exactly 7 days
    out is red — the stricter reading, which is the right way to round a
    penalty risk.
    """
    for label, upper in URGENCY_BANDS:
        if days_until_due <= upper:
            return label
    return URGENCY_DEFAULT


def financial_year_start(reference: date) -> date:
    """1 April of the financial year *reference* falls in."""
    year = reference.year if reference.month >= FY_START_MONTH else reference.year - 1
    return date(year, FY_START_MONTH, 1)


def financial_year_label(reference: date) -> str:
    """``"FY2026-27"`` for any date inside that year."""
    start = financial_year_start(reference)
    return f"FY{start.year}-{str(start.year + 1)[2:]}"


def financial_quarter(reference: date) -> int:
    """The financial quarter, 1-4. April-June is Q1, not January-March."""
    return ((reference.month - FY_START_MONTH) % 12) // 3 + 1


def last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def add_months(reference: date, months: int) -> date:
    """Shift by whole months, clamping the day to the target month's length.

    31 January plus one month is 28 February, not an exception. Every
    obligation whose due day is the 30th needs this in February.
    """
    total = reference.month - 1 + months
    year = reference.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(reference.day, last_day_of_month(year, month)))


@dataclass(frozen=True)
class Period:
    """One reporting period, and the handle a filing is stored under."""

    key: str
    start: date
    end: date
    frequency: Frequency

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.key


def month_period(reference: date) -> Period:
    """The calendar month containing *reference*. Key ``"2026-07"``."""
    start = date(reference.year, reference.month, 1)
    end = date(reference.year, reference.month, last_day_of_month(reference.year, reference.month))
    return Period(f"{start.year:04d}-{start.month:02d}", start, end, Frequency.MONTHLY)


def quarter_period(reference: date) -> Period:
    """The financial quarter containing *reference*. Key ``"FY2026-27-Q2"``."""
    quarter = financial_quarter(reference)
    fy_start = financial_year_start(reference)
    start = add_months(fy_start, (quarter - 1) * 3)
    end = add_months(start, 3) - timedelta(days=1)
    return Period(
        f"{financial_year_label(reference)}-Q{quarter}", start, end, Frequency.QUARTERLY
    )


def half_year_period(reference: date) -> Period:
    """The financial half containing *reference*. Key ``"FY2026-27-H1"``."""
    fy_start = financial_year_start(reference)
    half = 1 if financial_quarter(reference) <= 2 else 2
    start = add_months(fy_start, (half - 1) * 6)
    end = add_months(start, 6) - timedelta(days=1)
    return Period(
        f"{financial_year_label(reference)}-H{half}", start, end, Frequency.HALF_YEARLY
    )


def annual_period(reference: date) -> Period:
    """The financial year containing *reference*. Key ``"FY2026-27"``."""
    start = financial_year_start(reference)
    end = add_months(start, 12) - timedelta(days=1)
    return Period(financial_year_label(reference), start, end, Frequency.ANNUAL)


_PERIOD_BUILDERS = {
    Frequency.MONTHLY: month_period,
    Frequency.QUARTERLY: quarter_period,
    Frequency.HALF_YEARLY: half_year_period,
    Frequency.ANNUAL: annual_period,
}

_PERIOD_MONTHS = {
    Frequency.MONTHLY: 1,
    Frequency.QUARTERLY: 3,
    Frequency.HALF_YEARLY: 6,
    Frequency.ANNUAL: 12,
}


def period_for(frequency: Frequency, reference: date) -> Period | None:
    """The period of *frequency* containing *reference*.

    ``None`` for the two frequencies that have no periodic schedule —
    event-based and one-time obligations produce filings from events, not from
    the calendar, and asking this function for their period is a caller bug
    that should surface as a missing filing rather than a wrong one.
    """
    builder = _PERIOD_BUILDERS.get(frequency)
    return builder(reference) if builder else None


def periods_between(frequency: Frequency, start: date, end: date) -> list[Period]:
    """Every period of *frequency* that overlaps ``[start, end]``.

    Walks forward from the period containing *start*. The loop advances by the
    period's own length, so a year-long window yields 12 months or 4 quarters
    without the caller doing the arithmetic.
    """
    first = period_for(frequency, start)
    if first is None or start > end:
        return []

    step = _PERIOD_MONTHS[frequency]
    out: list[Period] = []
    cursor = first
    while cursor.start <= end:
        out.append(cursor)
        next_start = add_months(cursor.start, step)
        following = period_for(frequency, next_start)
        if following is None or following.start <= cursor.start:
            # Defensive: a builder that failed to advance would loop forever,
            # and this function is called from a scheduled sweep where that
            # means a pinned worker rather than a visible error.
            break
        cursor = following
    return out


@dataclass(frozen=True)
class DueDateRule:
    """The obligation fields that determine a due date.

    A value object rather than the ORM row, so the arithmetic can be tested
    without a database and so an organization's per-client overrides can be
    folded in before the calculation rather than during it.
    """

    frequency: Frequency
    due_day: int | None = None
    due_month: int | None = None
    offset_days: int | None = None
    period_offset: int = 1

    @classmethod
    def from_obligation(cls, obligation, *, override=None) -> DueDateRule:
        """Build a rule from a catalogue row plus an optional per-org override."""
        frequency = obligation.frequency
        due_day = obligation.due_day
        if override is not None:
            if override.frequency_override is not None:
                frequency = override.frequency_override
            if override.due_day_override is not None:
                due_day = override.due_day_override
        return cls(
            frequency=frequency,
            due_day=due_day,
            due_month=obligation.due_month,
            offset_days=obligation.offset_days,
            period_offset=obligation.period_offset,
        )


def due_date_for(rule: DueDateRule, period: Period) -> date:
    """When the return for *period* must be filed.

    Three rules, tried in order:

    1. **Offset from period end.** ``offset_days`` set — SEBI's shareholding
       pattern, due 21 days after the quarter ends. Also the natural rule for
       anything the regulator words as "within N days of".
    2. **Fixed month and day.** ``due_month`` set — MGT-7, due 31 October
       regardless of when the year ended. The month is read in the financial
       year ``period_offset`` years after the period's, which for the usual
       value of 1 is the year following the one being reported.
    3. **Day of the Nth following month.** Neither set, ``due_day`` set —
       GSTR-3B for July, due the 20th of August.

    With none of them set the due date is the period end itself. That is the
    conservative default: it can only ever be earlier than the truth, so it
    over-warns rather than letting a deadline pass silently.
    """
    if rule.offset_days is not None:
        return period.end + timedelta(days=rule.offset_days)

    if rule.due_month is not None:
        day = rule.due_day or 31
        # Anchor on the period's *start* year: a financial year that ends in
        # March 2027 belongs to FY2026-27, and "October of the following year"
        # means October 2027.
        year = period.start.year + rule.period_offset
        # A due_month before April belongs to the following calendar year,
        # because the financial year has rolled over by then. FY2026-27's
        # January deadline is January 2028 when period_offset is 1.
        if rule.due_month < FY_START_MONTH:
            year += 1
        return date(year, rule.due_month, min(day, last_day_of_month(year, rule.due_month)))

    if rule.due_day is not None:
        step = _PERIOD_MONTHS.get(rule.frequency, 1)
        target = add_months(period.start, step * rule.period_offset)
        return date(
            target.year,
            target.month,
            min(rule.due_day, last_day_of_month(target.year, target.month)),
        )

    return period.end


def next_due_date(rule: DueDateRule, *, today: date | None = None) -> date | None:
    """The next due date at or after *today*, or ``None`` for a non-periodic rule.

    Walks forward from the current period rather than solving analytically:
    the rules above are irregular enough that a closed form would be a source
    of off-by-one bugs, and at most a handful of iterations are ever needed.
    """
    reference = today or date.today()
    period = period_for(rule.frequency, reference)
    if period is None:
        return None

    step = _PERIOD_MONTHS[rule.frequency]
    cursor = period
    # Two periods back, in case a long ``period_offset`` puts an older period's
    # deadline still in the future.
    cursor = period_for(rule.frequency, add_months(cursor.start, -2 * step)) or cursor

    for _ in range(64):
        due = due_date_for(rule, cursor)
        if due >= reference:
            return due
        following = period_for(rule.frequency, add_months(cursor.start, step))
        if following is None:
            break
        cursor = following
    return None


def event_due_date(event_date: date, offset_days: int) -> date:
    """Due date for an event-based obligation — FC-GPR, 30 days from allotment."""
    return event_date + timedelta(days=offset_days)


def event_period_key(event_date: date) -> str:
    """Period key for an event-based filing: the ISO date of the event itself.

    Event-based obligations have no period, but the uniqueness constraint on
    ``filings`` needs a key. The event date serves: two allotments on different
    days are two filings, and two records of the same allotment are one.
    """
    return event_date.isoformat()
