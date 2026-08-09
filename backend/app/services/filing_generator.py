"""Turning the obligations an organization owes into dated filings on a calendar.

This is the bridge between three modules that each know one third of the
problem: :mod:`app.services.applicability` knows *what* is owed,
:mod:`app.services.deadline_engine` knows *when* a period's return is due, and
:mod:`app.models.filing` is where the answer is stored.

**Idempotence is the whole contract.** The nightly sweep runs against every
organization, and it will overlap with a manual "refresh my calendar" and with
a retry after a worker died halfway. Running it twice must not produce two
GSTR-3Bs for July. That is guaranteed twice over: the generator checks for an
existing filing before inserting, and a partial unique index on
``(organization_id, obligation_id, period_key)`` catches the race the check
cannot — two workers that both looked, both saw nothing, and both inserted.

**It only ever creates.** A filing someone has started work on is never
touched, and a period that has been marked not applicable stays that way. The
generator's job is to make sure nothing is missing, not to make the calendar
match its idea of the truth.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import FilingStatus, Frequency
from app.models.filing import Filing
from app.models.obligation import ComplianceObligation, OrganizationObligation
from app.models.organization import Organization
from app.services.applicability import applicable_obligations
from app.services.deadline_engine import (
    DueDateRule,
    Period,
    due_date_for,
    event_due_date,
    event_period_key,
    periods_between,
)
from app.services.filing_workflow import ensure_deadline

logger = logging.getLogger(__name__)

# How far ahead the sweep generates. Long enough that a quarterly return is on
# the calendar before anyone needs to start it, short enough that a client's
# calendar is not filled with rows for periods whose rules may change first —
# GST due dates in particular are amended mid-year often enough that
# generating two years out would mean generating them wrong.
DEFAULT_HORIZON_DAYS = 120

# How far back to fill in. A client onboarded today has open obligations from
# the current period, and a CA needs last month's missed GSTR-3B to appear so
# it can be dealt with. Not longer: back-filling a year would present a new
# client with 60 overdue rows nobody intends to file.
DEFAULT_LOOKBACK_DAYS = 45


@dataclass
class GenerationResult:
    """What one generation run produced."""

    organization_id: int
    created: int = 0
    # Periods that already had a filing. Counted so the log distinguishes "the
    # sweep did nothing because everything exists" from "the sweep did nothing
    # because it found no applicable obligations", which look identical
    # otherwise and mean very different things.
    skipped_existing: int = 0
    # Lost the insert race to a concurrent worker. Not an error.
    skipped_conflict: int = 0
    obligations_considered: int = 0
    filing_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "organization_id": self.organization_id,
            "created": self.created,
            "skipped_existing": self.skipped_existing,
            "skipped_conflict": self.skipped_conflict,
            "obligations_considered": self.obligations_considered,
        }


def _rule_for(
    obligation: ComplianceObligation, link: OrganizationObligation | None
) -> DueDateRule:
    return DueDateRule.from_obligation(obligation, override=link)


def _existing_period_keys(
    db: Session, organization_id: int, obligation_id: int
) -> set[str]:
    """Period keys already on the calendar for one obligation.

    Fetched per obligation rather than one row at a time: an organization with
    30 obligations over a four-month horizon is ~120 period checks, and doing
    those as 120 queries is what makes a nightly sweep across 400 clients slow
    enough to matter.

    Soft-deleted filings are *included*. A filing someone voided must not be
    silently recreated by the next sweep — that would make deletion impossible
    to sustain, and the partial unique index would let the insert through.
    """
    rows = db.execute(
        select(Filing.period_key).where(
            Filing.organization_id == organization_id,
            Filing.obligation_id == obligation_id,
        )
    ).scalars()
    return set(rows)


def _create_filing(
    db: Session,
    org_id: int,
    obligation: ComplianceObligation,
    *,
    period_key: str,
    due_date: date,
    period: Period | None = None,
) -> Filing | None:
    """Insert one filing, or return ``None`` if a concurrent writer won the race.

    The ``begin_nested`` is the same device :func:`app.services.audit.record`
    uses and for the same reason: a unique-constraint violation must roll back
    only this INSERT, not the caller's whole sweep. Without it, one collision
    partway through an organization's generation would abort the transaction
    and lose every filing created before it.
    """
    filing = Filing(
        organization_id=org_id,
        obligation_id=obligation.id,
        regulation=obligation.regulation,
        filing_type=obligation.filing_type,
        period_key=period_key,
        period_start=period.start if period else None,
        period_end=period.end if period else None,
        due_date=due_date,
        status=FilingStatus.NOT_STARTED,
    )
    db.add(filing)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        db.expunge(filing)
        return None

    ensure_deadline(db, filing)
    return filing


def generate_for_organization(
    db: Session,
    org: Organization,
    *,
    today: date | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> GenerationResult:
    """Create any missing filings for *org* across the generation window.

    Event-based and one-time obligations are skipped: they have no period, and
    their filings are produced by :func:`record_event_filing` when the event
    that triggers them is recorded. Generating them from the calendar would put
    an FC-GPR on every client's dashboard whether or not there was ever an
    allotment.

    Flushes, does not commit.
    """
    reference = today or date.today()
    window_start = reference - timedelta(days=lookback_days)
    window_end = reference + timedelta(days=horizon_days)
    result = GenerationResult(organization_id=org.id)

    for link, obligation in applicable_obligations(db, org.id):
        result.obligations_considered += 1
        rule = _rule_for(obligation, link)

        if rule.frequency in (Frequency.EVENT_BASED, Frequency.ONE_TIME):
            continue

        seen = _existing_period_keys(db, org.id, obligation.id)

        # Periods are enumerated over a window widened backwards by one year.
        # A period's *due date* is what has to land in the window, and an
        # annual return's due date falls in the year after its period — so
        # enumerating only periods inside the window would miss FY2025-26's
        # return entirely while generating FY2026-27's, which is not yet due.
        candidates = periods_between(
            rule.frequency, window_start - timedelta(days=400), window_end
        )

        for period in candidates:
            due = due_date_for(rule, period)
            if not (window_start <= due <= window_end):
                continue
            # An obligation that had not come into force when the period ended
            # was not owed for that period, whatever today's date is.
            if obligation.effective_from and period.end < obligation.effective_from:
                continue
            if obligation.effective_to and period.start > obligation.effective_to:
                continue
            if period.key in seen:
                result.skipped_existing += 1
                continue

            filing = _create_filing(
                db, org.id, obligation, period_key=period.key, due_date=due, period=period
            )
            if filing is None:
                result.skipped_conflict += 1
                continue
            seen.add(period.key)
            result.created += 1
            result.filing_ids.append(filing.id)

    db.flush()
    logger.info("Generated filings for org %s: %s", org.id, result.as_dict())
    return result


def record_event_filing(
    db: Session,
    org: Organization,
    obligation: ComplianceObligation,
    *,
    event_date: date,
    data: dict | None = None,
) -> Filing | None:
    """Create the filing an event-based obligation demands.

    FC-GPR is due 30 days after an allotment; there is no allotment until
    someone records one. Returns ``None`` when the filing already exists, which
    is what makes recording the same allotment twice harmless.

    The due-date offset comes from the obligation's ``offset_days``, which
    :class:`app.data.catalogue.base.ObligationSpec` requires every event-based
    entry to set — so there is no silent fallback to a wrong number here.
    """
    period_key = event_period_key(event_date)
    existing = db.execute(
        select(Filing).where(
            Filing.organization_id == org.id,
            Filing.obligation_id == obligation.id,
            Filing.period_key == period_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    due = event_due_date(event_date, obligation.offset_days or 30)
    filing = _create_filing(
        db, org.id, obligation, period_key=period_key, due_date=due
    )
    if filing is None:
        return None

    filing.period_start = event_date
    filing.period_end = event_date
    if data:
        filing.data_json = data
    db.flush()
    return filing


def generate_all(
    db: Session,
    *,
    today: date | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    organization_ids: list[int] | None = None,
) -> list[GenerationResult]:
    """Run the generator across every active organization.

    The nightly sweep's entry point. Each organization is committed
    independently: one client with a malformed profile must not stop the other
    399 from getting their calendar, and a sweep that rolled everything back on
    the last failure would be a sweep that never succeeds.
    """
    stmt = select(Organization).where(
        Organization.deleted_at.is_(None), Organization.is_active.is_(True)
    )
    if organization_ids:
        stmt = stmt.where(Organization.id.in_(organization_ids))

    results: list[GenerationResult] = []
    for org in db.execute(stmt.order_by(Organization.id)).scalars().all():
        try:
            results.append(
                generate_for_organization(
                    db, org, today=today, horizon_days=horizon_days
                )
            )
            db.commit()
        except Exception:  # noqa: BLE001 - one tenant must not stop the sweep
            db.rollback()
            logger.exception("Filing generation failed for organization %s", org.id)
    return results
