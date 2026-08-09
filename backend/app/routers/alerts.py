"""Alerts: notification history, channel preferences, and regulatory impacts.

Three things that share a router because they are one thing to the user — the
bell icon and what sits behind it.

The regulatory side is the part that matters commercially. A circular published
by the CBIC is a :class:`RegulatoryUpdate`; what it *means for one client* is a
:class:`RegulatoryImpact`, produced by the impact mapper cross-referencing the
update against each organization's profile. A CA firm's value is in the second,
not the first — everyone can read the circular.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_tenant_context, require_manager, require_writer
from app.core.errors import ForbiddenError, NotFoundError
from app.core.tenancy import TenantContext, scoped
from app.models.dpdp import BreachIncident, DataSubjectRequest
from app.models.enums import (
    AuditAction,
    FilingStatus,
    ImpactLevel,
    NotificationChannel,
    NotificationStatus,
    UserRole,
)
from app.models.filing import Filing
from app.models.mixins import utcnow
from app.models.notification import Notification, NotificationPreference
from app.models.regulatory import RegulatoryImpact, RegulatoryUpdate
from app.routers._helpers import paginate, record
from app.schemas.common import Page
from app.schemas.notification import (
    AcknowledgeRequest,
    AlertSummary,
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
    NotificationResponse,
    RegulatoryImpactResponse,
    RegulatoryUpdateResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts", tags=["alerts"])

_OPEN_FILING_STATUSES = (
    FilingStatus.NOT_STARTED,
    FilingStatus.DRAFT,
    FilingStatus.IN_REVIEW,
    FilingStatus.APPROVED,
    FilingStatus.REJECTED,
)


@router.get("/summary", response_model=AlertSummary, summary="What needs attention")
def alert_summary(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    today: date | None = Query(None),
):
    """The bell icon's badge, in one round trip.

    Seven counts rather than seven endpoints, because the alternative is the
    header firing seven requests on every page load and the user seeing them
    populate one at a time.
    """
    reference = today or date.today()
    week_out = reference + timedelta(days=7)

    def count(stmt) -> int:
        return int(
            db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
        )

    open_filings = scoped(Filing, ctx).where(Filing.status.in_(_OPEN_FILING_STATUSES))

    overdue = count(
        open_filings.where(
            func.coalesce(Filing.extended_due_date, Filing.due_date) < reference
        )
    )
    due_soon = count(
        open_filings.where(
            func.coalesce(Filing.extended_due_date, Filing.due_date) >= reference,
            func.coalesce(Filing.extended_due_date, Filing.due_date) <= week_out,
        )
    )

    impacts = scoped(RegulatoryImpact, ctx).where(
        RegulatoryImpact.is_acknowledged.is_(False)
    )
    breaches = scoped(BreachIncident, ctx).where(BreachIncident.closed_at.is_(None))
    requests = scoped(DataSubjectRequest, ctx).where(
        DataSubjectRequest.status.notin_(["completed", "rejected"])
    )

    return AlertSummary(
        unacknowledged_impacts=count(impacts),
        critical_impacts=count(
            impacts.where(RegulatoryImpact.impact_level == ImpactLevel.CRITICAL)
        ),
        overdue_filings=overdue,
        due_within_7_days=due_soon,
        open_breach_incidents=count(breaches),
        overdue_data_requests=count(
            requests.where(DataSubjectRequest.due_date < reference)
        ),
        failed_notifications=count(
            scoped(Notification, ctx).where(
                Notification.status == NotificationStatus.FAILED
            )
        ),
    )


@router.get(
    "/notifications",
    response_model=Page[NotificationResponse],
    summary="Notification history",
)
def list_notifications(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    channel: NotificationChannel | None = None,
    status: NotificationStatus | None = None,
    kind: str | None = Query(None, max_length=64),
):
    """Everything CompliPilot sent this organization, or tried to.

    Including the failures. A reminder that did not go out is the thing a
    client will point at after a missed deadline, and a history that showed
    only successes could not answer them.
    """
    stmt = scoped(Notification, ctx)
    if channel is not None:
        stmt = stmt.where(Notification.channel == channel)
    if status is not None:
        stmt = stmt.where(Notification.status == status)
    if kind:
        stmt = stmt.where(Notification.kind == kind)

    stmt = stmt.order_by(Notification.created_at.desc(), Notification.id.desc())
    rows, total = paginate(db, stmt, limit=limit, offset=offset)
    return Page[NotificationResponse](
        items=[NotificationResponse.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def _preference_row(
    db: Session, ctx: TenantContext, user_id: int | None
) -> NotificationPreference | None:
    return db.execute(
        scoped(NotificationPreference, ctx).where(
            NotificationPreference.user_id.is_(None)
            if user_id is None
            else NotificationPreference.user_id == user_id
        )
    ).scalar_one_or_none()


@router.get(
    "/preferences",
    response_model=NotificationPreferenceResponse,
    summary="How this organization is reached",
)
def get_preferences(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    mine: bool = Query(False, description="Your personal override rather than the org default"),
):
    """The organization default, or one user's override.

    Creates the row on first read rather than returning a 404. A preferences
    screen that has to handle "no preferences exist yet" as a distinct state is
    a screen with a bug in it, and the defaults on the model are exactly what
    the row would hold.
    """
    user_id = ctx.user_id if mine else None
    row = _preference_row(db, ctx, user_id)
    if row is None:
        row = NotificationPreference(organization_id=ctx.org_id, user_id=user_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return NotificationPreferenceResponse.model_validate(row)


@router.patch(
    "/preferences",
    response_model=NotificationPreferenceResponse,
    summary="Change how this organization is reached",
)
def update_preferences(
    payload: NotificationPreferenceUpdate,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
    mine: bool = Query(False, description="Your personal override rather than the org default"),
):
    """Update the organization default, or your own override.

    Your own override needs only write access — it affects nobody else.
    Changing the *organization* default requires Compliance Manager: turning
    off every channel for a whole tenant would silently stop every deadline
    reminder for every user, which is not a change a Staff account should be
    able to make. Either way it is recorded with its before and after, because
    "we never got the reminder" is answerable only from that record.
    """
    user_id = ctx.user_id if mine else None
    if not mine and not ctx.at_least(UserRole.COMPLIANCE_MANAGER):
        raise ForbiddenError(
            "Changing the organization's notification defaults requires the "
            "compliance_manager role",
            details={"your_role": str(ctx.role), "hint": "Pass mine=true for your own"},
        )

    row = _preference_row(db, ctx, user_id)
    if row is None:
        row = NotificationPreference(organization_id=ctx.org_id, user_id=user_id)
        db.add(row)
        db.flush()

    audited = (
        "email_enabled",
        "whatsapp_enabled",
        "sms_enabled",
        "in_app_enabled",
        "email_address",
        "whatsapp_number",
        "sms_number",
        "reminder_offsets_json",
        "quiet_hours_start",
        "quiet_hours_end",
    )
    before = {f: getattr(row, f) for f in audited}

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)

    after = {f: getattr(row, f) for f in audited}
    diff = {k: {"from": before[k], "to": after[k]} for k in audited if before[k] != after[k]}
    if diff:
        record(
            db,
            ctx,
            request,
            action=AuditAction.UPDATE,
            entity_type="notification_preference",
            entity_id=row.id,
            before={k: v["from"] for k, v in diff.items()},
            after={k: v["to"] for k, v in diff.items()},
            summary=(
                ("Personal" if mine else "Organization")
                + f" notification preferences updated: {', '.join(sorted(diff))}"
            ),
        )
    db.commit()
    db.refresh(row)
    return NotificationPreferenceResponse.model_validate(row)


@router.get(
    "/regulatory",
    response_model=Page[RegulatoryImpactResponse],
    summary="Regulatory changes that affect this organization",
)
def list_impacts(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    impact_level: ImpactLevel | None = None,
    unacknowledged_only: bool = Query(False),
):
    """Per-client regulatory impacts, worst first.

    Ordered by impact level rather than by date, because a CRITICAL change
    published last week matters more than a LOW one published yesterday, and a
    reverse-chronological list buries it.
    """
    stmt = scoped(RegulatoryImpact, ctx).join(
        RegulatoryUpdate, RegulatoryUpdate.id == RegulatoryImpact.update_id
    )
    if impact_level is not None:
        stmt = stmt.where(RegulatoryImpact.impact_level == impact_level)
    if unacknowledged_only:
        stmt = stmt.where(RegulatoryImpact.is_acknowledged.is_(False))

    # StrEnum members sort alphabetically, which is not the severity order, so
    # the ranking is spelled out rather than left to the column.
    severity = {
        ImpactLevel.CRITICAL: 0,
        ImpactLevel.HIGH: 1,
        ImpactLevel.MEDIUM: 2,
        ImpactLevel.LOW: 3,
        ImpactLevel.NONE: 4,
    }
    stmt = stmt.order_by(RegulatoryUpdate.published_date.desc())
    rows, total = paginate(db, stmt, limit=limit, offset=offset)
    rows.sort(key=lambda r: (severity.get(r.impact_level, 9), -r.id))

    items = []
    for impact in rows:
        response = RegulatoryImpactResponse.model_validate(impact)
        if impact.update is not None:
            response.update = RegulatoryUpdateResponse.model_validate(impact.update)
        items.append(response)

    return Page[RegulatoryImpactResponse](
        items=items, total=total, limit=limit, offset=offset
    )


@router.post(
    "/regulatory/{impact_id}/acknowledge",
    response_model=RegulatoryImpactResponse,
    summary="Acknowledge a regulatory impact",
)
def acknowledge_impact(
    impact_id: int,
    payload: AcknowledgeRequest,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Record that a human has seen and accepted a regulatory change.

    Compliance Manager and above. An acknowledgement is a statement that
    somebody competent read the circular and decided what to do, and it is
    exactly the record a regulator asks for after the fact — so it must not be
    something a read-only account can click away.
    """
    impact = db.execute(
        scoped(RegulatoryImpact, ctx).where(RegulatoryImpact.id == impact_id)
    ).scalar_one_or_none()
    if impact is None:
        raise NotFoundError("No such regulatory impact")

    if not impact.is_acknowledged:
        impact.is_acknowledged = True
        impact.acknowledged_at = utcnow()
        impact.acknowledged_by_id = ctx.user_id

        record(
            db,
            ctx,
            request,
            action=AuditAction.UPDATE,
            entity_type="regulatory_impact",
            entity_id=impact.id,
            after={
                "is_acknowledged": True,
                "acknowledged_by_id": ctx.user_id,
                "notes": payload.notes,
            },
            summary=f"Regulatory impact {impact.id} acknowledged",
        )
        db.commit()
        db.refresh(impact)

    response = RegulatoryImpactResponse.model_validate(impact)
    if impact.update is not None:
        response.update = RegulatoryUpdateResponse.model_validate(impact.update)
    return response


@router.get(
    "/regulatory/updates",
    response_model=Page[RegulatoryUpdateResponse],
    summary="The published regulatory feed",
)
def list_updates(
    _ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    since: date | None = Query(None),
):
    """The regulatory feed itself, unfiltered by organization.

    Not tenant-scoped, because a circular is public — it is the *impact* that
    is private. Only published, analysed rows are returned: an update the
    pipeline has ingested but not yet analysed has no summary and no impact
    level, and showing it would be showing raw scraped text.
    """
    stmt = select(RegulatoryUpdate).where(
        RegulatoryUpdate.deleted_at.is_(None),
        RegulatoryUpdate.is_published.is_(True),
    )
    if since is not None:
        stmt = stmt.where(RegulatoryUpdate.published_date >= since)

    stmt = stmt.order_by(RegulatoryUpdate.published_date.desc(), RegulatoryUpdate.id.desc())
    rows, total = paginate(db, stmt, limit=limit, offset=offset)
    return Page[RegulatoryUpdateResponse](
        items=[RegulatoryUpdateResponse.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
