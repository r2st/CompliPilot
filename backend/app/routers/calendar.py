"""The compliance calendar (section 4.1).

A calendar is not a filings list with a date filter, which is why it has its
own router and its own item schema. Two differences drive that:

* **It is a union.** Filings are most of it, but a deadline extracted from a
  parsed show-cause notice belongs on the same calendar, and so does a DPDP
  breach's 72-hour clock. ``CalendarItem.source`` says which.
* **It is aggregated.** The header strip needs the counts — how many overdue,
  how many this week, how they split by urgency band — and computing those from
  a paginated list on the client gives the wrong answer for every page but the
  first.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_tenant_context, require_manager
from app.core.errors import NotFoundError, ValidationError
from app.core.tenancy import TenantContext, accessible_client_org_ids
from app.models.document import Document
from app.models.dpdp import BREACH_NOTIFICATION_HOURS, BreachIncident
from app.models.enums import AuditAction, FilingStatus, OrgType, Regulation
from app.models.filing import Filing
from app.models.obligation import ComplianceObligation
from app.models.organization import Organization
from app.routers._helpers import record
from app.schemas.filing import CalendarItem, CalendarResponse, GenerationResponse
from app.services import filing_generator
from app.services.deadline_engine import urgency

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calendar", tags=["calendar"])

# The default window: a month back so overdue items stay visible, three months
# forward so a quarterly return appears before anyone needs to start it.
_DEFAULT_LOOKBACK = 30
_DEFAULT_HORIZON = 90

# A window wider than this is a report, not a calendar, and building one across
# a CA firm's whole client book is the query most likely to exhaust the pool.
_MAX_WINDOW_DAYS = 800


def _filing_items(
    db: Session,
    org_ids: list[int],
    start: date,
    end: date,
    today: date,
    *,
    org_names: dict[int, str],
    regulation: Regulation | None,
    include_closed: bool,
) -> list[CalendarItem]:
    """Filings whose effective due date falls in the window.

    Joined to the obligation for the title and the per-day penalty, because
    "₹200/day" beside a deadline is what actually moves an SMB to file — and
    fetching it per row would be one query per calendar entry.
    """
    stmt = (
        select(Filing, ComplianceObligation)
        .join(ComplianceObligation, ComplianceObligation.id == Filing.obligation_id)
        .where(
            Filing.organization_id.in_(org_ids),
            Filing.deleted_at.is_(None),
        )
    )
    if regulation is not None:
        stmt = stmt.where(Filing.regulation == regulation)
    if not include_closed:
        stmt = stmt.where(
            Filing.status.notin_(
                [
                    FilingStatus.ACKNOWLEDGED,
                    FilingStatus.NOT_APPLICABLE,
                ]
            )
        )

    items: list[CalendarItem] = []
    for filing, obligation in db.execute(stmt).all():
        # The window test is on the *effective* due date, which an extension
        # may have moved. Filtering in SQL would need the same coalesce in two
        # places and would still not give the derived fields below, so the
        # window is applied here against a set already narrowed by tenant.
        due = filing.effective_due_date
        if not (start <= due <= end):
            continue
        days = (due - today).days
        items.append(
            CalendarItem(
                date=due,
                source="filing",
                filing_id=filing.id,
                organization_id=filing.organization_id,
                organization_name=org_names.get(filing.organization_id),
                regulation=filing.regulation,
                filing_type=filing.filing_type,
                title=obligation.title,
                period_key=filing.period_key,
                status=filing.status,
                days_until_due=days,
                urgency=urgency(days),
                penalty_per_day_paise=obligation.penalty_per_day_paise,
                is_open=filing.is_open,
            )
        )
    return items


def _document_items(
    db: Session,
    org_ids: list[int],
    start: date,
    end: date,
    today: date,
    org_names: dict[int, str],
) -> list[CalendarItem]:
    """Deadlines the parser extracted from uploaded notices.

    A show-cause notice with a 15-day reply window is a real deadline with a
    real consequence, and it exists nowhere in the obligations catalogue
    because nobody could have predicted it. Leaving it off the calendar would
    mean the one deadline nobody scheduled is also the one nobody sees.
    """
    rows = db.execute(
        select(Document).where(
            Document.organization_id.in_(org_ids),
            Document.deleted_at.is_(None),
            Document.extracted_deadline.is_not(None),
            Document.extracted_deadline >= start,
            Document.extracted_deadline <= end,
        )
    ).scalars()

    items = []
    for doc in rows:
        assert doc.extracted_deadline is not None  # narrowed by the query
        days = (doc.extracted_deadline - today).days
        items.append(
            CalendarItem(
                date=doc.extracted_deadline,
                source="document",
                organization_id=doc.organization_id,
                organization_name=org_names.get(doc.organization_id),
                regulation=doc.regulation,
                title=doc.title,
                days_until_due=days,
                urgency=urgency(days),
            )
        )
    return items


def _breach_items(
    db: Session,
    org_ids: list[int],
    start: date,
    end: date,
    today: date,
    org_names: dict[int, str],
) -> list[CalendarItem]:
    """The DPDP 72-hour Data Protection Board notification clock.

    Only for breaches not yet notified. A breach that has been reported has no
    remaining deadline, and leaving it on the calendar would make the register
    read as permanently overdue.
    """
    rows = db.execute(
        select(BreachIncident).where(
            BreachIncident.organization_id.in_(org_ids),
            BreachIncident.deleted_at.is_(None),
            BreachIncident.dpb_notified_at.is_(None),
        )
    ).scalars()

    items = []
    for breach in rows:
        deadline = (
            breach.detected_at + timedelta(hours=BREACH_NOTIFICATION_HOURS)
        ).date()
        if not (start <= deadline <= end):
            continue
        days = (deadline - today).days
        items.append(
            CalendarItem(
                date=deadline,
                source="breach",
                organization_id=breach.organization_id,
                organization_name=org_names.get(breach.organization_id),
                regulation=Regulation.DPDP,
                title=f"Notify the Data Protection Board: {breach.title}",
                days_until_due=days,
                urgency=urgency(days),
            )
        )
    return items


def _resolve_scope(
    db: Session, ctx: TenantContext, all_clients: bool
) -> tuple[list[int], dict[int, str]]:
    """The organizations this calendar covers, and their names.

    ``all_clients`` is the CA firm's consolidated calendar — every client they
    may reach, in one view. The list comes from
    :func:`app.core.tenancy.accessible_client_org_ids`, which already applies
    the assignment rules, so a Staff member's "all clients" is only theirs.
    """
    if not all_clients:
        org_ids = [ctx.org_id]
    else:
        home = db.get(Organization, ctx.home_org_id)
        if home is None or home.type != OrgType.CA_FIRM:
            raise ValidationError(
                "A consolidated calendar is only available to a CA firm"
            )
        org_ids = accessible_client_org_ids(db, ctx.user)
        # The firm's own obligations belong on it too — a practice files its
        # own GST returns like any other business.
        org_ids.append(ctx.home_org_id)

    names = {
        row.id: row.name
        for row in db.execute(
            select(Organization).where(Organization.id.in_(org_ids))
        ).scalars()
    }
    return org_ids, names


@router.get("", response_model=CalendarResponse, summary="The compliance calendar")
def get_calendar(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    start: date | None = Query(None, description="Window start; defaults to 30 days ago"),
    end: date | None = Query(None, description="Window end; defaults to 90 days ahead"),
    regulation: Regulation | None = None,
    include_closed: bool = Query(False, description="Include acknowledged filings"),
    all_clients: bool = Query(
        False, description="CA firms: every client in one calendar"
    ),
    today: date | None = Query(
        None, description="Override the reference date; for testing and back-dating"
    ),
):
    """Everything dated in a window, from every source that produces a deadline."""
    reference = today or date.today()
    window_start = start or reference - timedelta(days=_DEFAULT_LOOKBACK)
    window_end = end or reference + timedelta(days=_DEFAULT_HORIZON)

    if window_end < window_start:
        raise ValidationError("The calendar window ends before it starts")
    if (window_end - window_start).days > _MAX_WINDOW_DAYS:
        raise ValidationError(
            f"A calendar window may span at most {_MAX_WINDOW_DAYS} days",
            details={"requested_days": (window_end - window_start).days},
        )

    org_ids, names = _resolve_scope(db, ctx, all_clients)

    items = _filing_items(
        db,
        org_ids,
        window_start,
        window_end,
        reference,
        org_names=names,
        regulation=regulation,
        include_closed=include_closed,
    )
    if regulation is None or regulation == Regulation.DPDP:
        items += _breach_items(db, org_ids, window_start, window_end, reference, names)
    items += [
        item
        for item in _document_items(
            db, org_ids, window_start, window_end, reference, names
        )
        if regulation is None or item.regulation == regulation
    ]

    items.sort(key=lambda i: (i.date, i.organization_id, i.title))

    by_urgency: dict[str, int] = {}
    by_regulation: dict[str, int] = {}
    for item in items:
        by_urgency[item.urgency] = by_urgency.get(item.urgency, 0) + 1
        if item.regulation is not None:
            key = str(item.regulation)
            by_regulation[key] = by_regulation.get(key, 0) + 1

    week_out = reference + timedelta(days=7)
    return CalendarResponse(
        start=window_start,
        end=window_end,
        items=items,
        total=len(items),
        overdue=sum(1 for i in items if i.days_until_due < 0 and i.is_open),
        due_this_week=sum(
            1 for i in items if reference <= i.date <= week_out and i.is_open
        ),
        by_urgency=by_urgency,
        by_regulation=by_regulation,
    )


@router.post(
    "/generate",
    response_model=GenerationResponse,
    summary="Generate the filings this organization owes",
)
def generate(
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
    horizon_days: int = Query(
        filing_generator.DEFAULT_HORIZON_DAYS, ge=1, le=400
    ),
    today: date | None = Query(None),
):
    """Create any missing filings across the generation window.

    Idempotent: running it twice does not produce two GSTR-3Bs for July. The
    nightly sweep calls the same function, so a user pressing this button is
    doing exactly what the scheduler does, not something adjacent to it.

    Requires that applicability has been evaluated first — the generator reads
    :func:`app.services.applicability.applicable_obligations`, which is empty
    until the sync has run, and an empty result here is reported honestly as
    zero rather than silently.
    """
    org = db.get(Organization, ctx.org_id)
    if org is None or org.deleted_at is not None:
        raise NotFoundError("No such organization")

    result = filing_generator.generate_for_organization(
        db, org, today=today, horizon_days=horizon_days
    )
    if result.created:
        record(
            db,
            ctx,
            request,
            action=AuditAction.GENERATE,
            entity_type="filing",
            after=result.as_dict(),
            summary=f"{result.created} filings generated from the compliance calendar",
        )
    db.commit()
    return GenerationResponse(**result.as_dict())
