"""Dashboards: one for an SMB, one consolidated for a CA firm (sections 4.1, 4.5).

The compliance score is the product's headline number, so it is worth saying
plainly how it is built and what it is not.

**It is a penalty-risk indicator, not a compliance certificate.** It measures
what CompliPilot can see: filings tracked here, impacts acknowledged here,
breaches logged here. A business with obligations it never told us about will
score well and be in trouble anyway, which is why the score always travels with
its components — a bare number invites exactly the misreading the components
prevent.

**Overdue is weighted far above late.** A return filed three days late cost a
fee and is finished. A return still open two weeks past its date is accruing
₹200 a day and nobody has noticed. The score has to move most on the second.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_tenant_context
from app.core.errors import ForbiddenError, NotFoundError
from app.core.tenancy import TenantContext, accessible_client_org_ids, scoped
from app.models.document import Document
from app.models.dpdp import BREACH_NOTIFICATION_HOURS, BreachIncident, DataSubjectRequest
from app.models.enums import FilingStatus, OrgType, ParseStatus, Regulation, UserRole, role_rank
from app.models.filing import Filing
from app.models.mixins import utcnow
from app.models.obligation import ComplianceObligation
from app.models.organization import Client, Organization
from app.models.regulatory import RegulatoryImpact
from app.models.user import User
from app.schemas.audit import (
    ClientDashboardRow,
    ComplianceScore,
    DashboardResponse,
    FirmDashboardResponse,
    RegulationBreakdown,
    WorkloadRow,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

OPEN_STATUSES = (
    FilingStatus.NOT_STARTED,
    FilingStatus.DRAFT,
    FilingStatus.IN_REVIEW,
    FilingStatus.APPROVED,
    FilingStatus.REJECTED,
)

FILED_ON_TIME = (FilingStatus.SUBMITTED, FilingStatus.ACKNOWLEDGED)

# How many points each condition costs. Overdue dominates, because it is the
# only one still accruing money. Tuned so that a business with everything filed
# scores 100 and one with a third of its calendar overdue lands in the 40s —
# a band a CA reads as "call this client today".
_PENALTY_PER_OVERDUE = 8
_PENALTY_PER_LATE = 2
_PENALTY_PER_UNACKNOWLEDGED_IMPACT = 3
_PENALTY_PER_MISSED_DPB_NOTIFICATION = 15

_BANDS: tuple[tuple[int, str], ...] = (
    (90, "excellent"),
    (75, "good"),
    (50, "at_risk"),
    (0, "critical"),
)


def band_for(score: int) -> str:
    for floor, label in _BANDS:
        if score >= floor:
            return label
    return "critical"


def _count(db: Session, stmt) -> int:
    return int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())


def compute_score(
    db: Session, org_id: int, today: date
) -> tuple[ComplianceScore, int]:
    """The compliance score for one organization, and its penalty exposure in paise.

    Returned together because both walk the same filings, and computing them
    separately would double the query count on the CA firm dashboard — which
    runs this once per client.
    """
    filings = list(
        db.execute(
            select(Filing, ComplianceObligation)
            .join(ComplianceObligation, ComplianceObligation.id == Filing.obligation_id)
            .where(Filing.organization_id == org_id, Filing.deleted_at.is_(None))
        ).all()
    )

    overdue = late = on_time = open_count = 0
    exposure = 0

    for filing, obligation in filings:
        if filing.status == FilingStatus.LATE_FILED:
            late += 1
        elif filing.status in FILED_ON_TIME:
            on_time += 1

        if filing.status not in OPEN_STATUSES:
            continue
        open_count += 1

        days_late = (today - filing.effective_due_date).days
        if days_late <= 0:
            continue
        overdue += 1

        # Exposure is the per-day penalty times days elapsed, capped at the
        # statutory maximum where the catalogue records one. An uncapped
        # accumulation would report a ₹200/day late fee on a two-year-old
        # return as ₹146,000 when the Act caps it at ₹10,000.
        if obligation.penalty_per_day_paise:
            accrued = obligation.penalty_per_day_paise * days_late
            if obligation.penalty_max_paise:
                accrued = min(accrued, obligation.penalty_max_paise)
            exposure += accrued

    unacknowledged = _count(
        db,
        select(RegulatoryImpact).where(
            RegulatoryImpact.organization_id == org_id,
            RegulatoryImpact.deleted_at.is_(None),
            RegulatoryImpact.is_acknowledged.is_(False),
        ),
    )
    missed_dpb = _count(
        db,
        select(BreachIncident).where(
            BreachIncident.organization_id == org_id,
            BreachIncident.deleted_at.is_(None),
            BreachIncident.dpb_notified_at.is_(None),
            BreachIncident.detected_at
            < utcnow() - timedelta(hours=BREACH_NOTIFICATION_HOURS),
        ),
    )

    score = 100
    score -= overdue * _PENALTY_PER_OVERDUE
    score -= late * _PENALTY_PER_LATE
    score -= unacknowledged * _PENALTY_PER_UNACKNOWLEDGED_IMPACT
    score -= missed_dpb * _PENALTY_PER_MISSED_DPB_NOTIFICATION
    score = max(0, min(100, score))

    return (
        ComplianceScore(
            score=score,
            band=band_for(score),
            on_time_filings=on_time,
            late_filings=late,
            overdue_filings=overdue,
            open_filings=open_count,
            total_filings=len(filings),
            unacknowledged_impacts=unacknowledged,
            overdue_dpb_notifications=missed_dpb,
        ),
        exposure,
    )


def _filing_card(filing: Filing, obligation: ComplianceObligation | None, today: date) -> dict:
    """The compact shape the dashboard's upcoming/overdue lists render."""
    return {
        "filing_id": filing.id,
        "title": obligation.title if obligation else filing.filing_type,
        "filing_type": filing.filing_type,
        "regulation": str(filing.regulation),
        "period_key": filing.period_key,
        "due_date": filing.effective_due_date.isoformat(),
        "days_until_due": filing.days_until(today),
        "status": str(filing.status),
        "penalty_per_day_paise": obligation.penalty_per_day_paise if obligation else None,
    }


@router.get("", response_model=DashboardResponse, summary="This organization's dashboard")
def get_dashboard(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    today: date | None = Query(None),
    upcoming_days: int = Query(30, ge=1, le=365),
    limit: int = Query(10, ge=1, le=50),
):
    """One organization's compliance position."""
    reference = today or date.today()
    org = db.get(Organization, ctx.org_id)
    if org is None or org.deleted_at is not None:
        raise NotFoundError("No such organization")

    score, exposure = compute_score(db, org.id, reference)

    open_stmt = (
        select(Filing, ComplianceObligation)
        .join(ComplianceObligation, ComplianceObligation.id == Filing.obligation_id)
        .where(
            Filing.organization_id == org.id,
            Filing.deleted_at.is_(None),
            Filing.status.in_(OPEN_STATUSES),
        )
        .order_by(Filing.due_date)
    )
    open_rows = list(db.execute(open_stmt).all())

    horizon = reference + timedelta(days=upcoming_days)
    upcoming = [
        _filing_card(f, o, reference)
        for f, o in open_rows
        if reference <= f.effective_due_date <= horizon
    ][:limit]
    overdue = [
        _filing_card(f, o, reference)
        for f, o in open_rows
        if f.effective_due_date < reference
    ][:limit]

    by_regulation: dict[str, dict[str, int]] = {}
    by_status: dict[str, int] = {}
    for filing, _ in db.execute(
        select(Filing, ComplianceObligation)
        .join(ComplianceObligation, ComplianceObligation.id == Filing.obligation_id)
        .where(Filing.organization_id == org.id, Filing.deleted_at.is_(None))
    ).all():
        key = str(filing.regulation)
        bucket = by_regulation.setdefault(
            key, {"total": 0, "open": 0, "overdue": 0, "submitted": 0}
        )
        bucket["total"] += 1
        if filing.status in OPEN_STATUSES:
            bucket["open"] += 1
            if filing.effective_due_date < reference:
                bucket["overdue"] += 1
        elif filing.status in (*FILED_ON_TIME, FilingStatus.LATE_FILED):
            bucket["submitted"] += 1
        by_status[str(filing.status)] = by_status.get(str(filing.status), 0) + 1

    return DashboardResponse(
        organization_id=org.id,
        organization_name=org.name,
        as_of=reference,
        score=score,
        upcoming=upcoming,
        overdue=overdue,
        by_regulation=[
            RegulationBreakdown(regulation=Regulation(key), **counts)
            for key, counts in sorted(by_regulation.items())
        ],
        by_status=by_status,
        penalty_exposure_paise=exposure,
        documents_pending_parse=_count(
            db,
            scoped(Document, ctx).where(
                Document.parse_status.in_([ParseStatus.PENDING, ParseStatus.PROCESSING])
            ),
        ),
        open_data_requests=_count(
            db,
            scoped(DataSubjectRequest, ctx).where(
                DataSubjectRequest.status.notin_(["completed", "rejected"])
            ),
        ),
        open_breaches=_count(
            db, scoped(BreachIncident, ctx).where(BreachIncident.closed_at.is_(None))
        ),
    )


def _require_firm(db: Session, ctx: TenantContext) -> Organization:
    firm = db.get(Organization, ctx.home_org_id)
    if firm is None or firm.deleted_at is not None:
        raise NotFoundError("No such organization")
    if firm.type != OrgType.CA_FIRM:
        raise ForbiddenError("The consolidated dashboard is for CA firms")
    return firm


@router.get(
    "/firm",
    response_model=FirmDashboardResponse,
    summary="A CA firm's consolidated dashboard",
)
def firm_dashboard(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    today: date | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
):
    """Every client the caller may see, scored and ranked.

    The client set comes from
    :func:`app.core.tenancy.accessible_client_org_ids`, so a Staff member's
    consolidated view is only the clients they are assigned to — the same rule
    the client list applies, enforced again rather than assumed.

    ``limit`` caps how many clients are scored. A firm past it gets the first
    N by name and a truthful ``client_count``; the per-client scoring walks
    each client's filings, and running it unbounded across a thousand clients
    is the request that would hold a connection long enough to matter.
    """
    reference = today or date.today()
    firm = _require_firm(db, ctx)

    org_ids = accessible_client_org_ids(db, ctx.user)
    engagements = list(
        db.execute(
            select(Client, Organization)
            .join(Organization, Organization.id == Client.client_org_id)
            .where(
                Client.ca_firm_id == firm.id,
                Client.deleted_at.is_(None),
                Client.client_org_id.in_(org_ids),
            )
            .order_by(Organization.name)
            .limit(limit)
        ).all()
    )

    rows: list[ClientDashboardRow] = []
    total_open = total_overdue = total_exposure = 0

    for engagement, org in engagements:
        score, exposure = compute_score(db, org.id, reference)

        next_filing = db.execute(
            select(Filing)
            .where(
                Filing.organization_id == org.id,
                Filing.deleted_at.is_(None),
                Filing.status.in_(OPEN_STATUSES),
                Filing.due_date >= reference,
            )
            .order_by(Filing.due_date)
            .limit(1)
        ).scalar_one_or_none()

        week_out = reference + timedelta(days=7)
        due_soon = _count(
            db,
            select(Filing).where(
                Filing.organization_id == org.id,
                Filing.deleted_at.is_(None),
                Filing.status.in_(OPEN_STATUSES),
                func.coalesce(Filing.extended_due_date, Filing.due_date) >= reference,
                func.coalesce(Filing.extended_due_date, Filing.due_date) <= week_out,
            ),
        )

        rows.append(
            ClientDashboardRow(
                client_id=engagement.id,
                organization_id=org.id,
                name=org.name,
                status=engagement.status,
                assigned_user_id=engagement.assigned_user_id,
                score=score.score,
                band=score.band,
                open_filings=score.open_filings,
                overdue_filings=score.overdue_filings,
                due_within_7_days=due_soon,
                penalty_exposure_paise=exposure,
                next_due_date=next_filing.effective_due_date if next_filing else None,
                next_filing_type=next_filing.filing_type if next_filing else None,
            )
        )
        total_open += score.open_filings
        total_overdue += score.overdue_filings
        total_exposure += exposure

    average = round(sum(r.score for r in rows) / len(rows)) if rows else 100

    # The firm's actual work queue: anything overdue, worst first, then by
    # money at risk. A different question from the alphabetical list above, and
    # the one somebody opens the app to answer.
    attention = sorted(
        (r for r in rows if r.overdue_filings > 0),
        key=lambda r: (-r.overdue_filings, -r.penalty_exposure_paise),
    )

    return FirmDashboardResponse(
        ca_firm_id=firm.id,
        ca_firm_name=firm.name,
        as_of=reference,
        client_count=len(org_ids),
        clients=rows,
        total_open_filings=total_open,
        total_overdue_filings=total_overdue,
        total_penalty_exposure_paise=total_exposure,
        average_score=average,
        attention_required=attention,
    )


@router.get(
    "/firm/workload",
    response_model=list[WorkloadRow],
    summary="How work is distributed across firm staff",
)
def firm_workload(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    today: date | None = Query(None),
):
    """Per-staff client and filing counts (section 4.5).

    Compliance Manager and above, because it names individuals and their
    backlogs. Enforced here rather than by a dependency so the message can say
    why.
    """
    reference = today or date.today()
    firm = _require_firm(db, ctx)

    if role_rank(ctx.role) < role_rank(UserRole.COMPLIANCE_MANAGER):
        raise ForbiddenError(
            "The workload report names individual staff and their backlogs; "
            "it requires the compliance_manager role"
        )

    staff = list(
        db.execute(
            select(User)
            .where(
                User.organization_id == firm.id,
                User.deleted_at.is_(None),
                User.is_active.is_(True),
            )
            .order_by(User.full_name)
        )
        .scalars()
        .all()
    )

    week_out = reference + timedelta(days=7)
    out: list[WorkloadRow] = []

    for member in staff:
        client_org_ids = list(
            db.execute(
                select(Client.client_org_id).where(
                    Client.ca_firm_id == firm.id,
                    Client.deleted_at.is_(None),
                    Client.assigned_user_id == member.id,
                )
            ).scalars()
        )

        open_filings = overdue = due_soon = 0
        if client_org_ids:
            base = select(Filing).where(
                Filing.organization_id.in_(client_org_ids),
                Filing.deleted_at.is_(None),
                Filing.status.in_(OPEN_STATUSES),
            )
            open_filings = _count(db, base)
            overdue = _count(
                db,
                base.where(
                    func.coalesce(Filing.extended_due_date, Filing.due_date) < reference
                ),
            )
            due_soon = _count(
                db,
                base.where(
                    func.coalesce(Filing.extended_due_date, Filing.due_date) >= reference,
                    func.coalesce(Filing.extended_due_date, Filing.due_date) <= week_out,
                ),
            )

        out.append(
            WorkloadRow(
                user_id=member.id,
                full_name=member.full_name,
                role=str(member.role),
                client_count=len(client_org_ids),
                open_filings=open_filings,
                overdue_filings=overdue,
                due_within_7_days=due_soon,
            )
        )

    return out
