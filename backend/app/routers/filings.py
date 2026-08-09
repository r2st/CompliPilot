"""Filings: the CRUD, the workflow transitions, and event-based creation.

Status is deliberately *not* writable through the PATCH. Every move goes
through ``POST /filings/{id}/transition``, which calls
:mod:`app.services.filing_workflow` — if status were a patchable field, every
guard in that module (legal transitions, role requirements, the
preparer-is-not-approver rule) could be skipped with an ordinary update.
"""
from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from fastapi import status as http_status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_tenant_context, require_manager, require_writer
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.tenancy import TenantContext, catalogue_scoped, scoped
from app.models.enums import AuditAction, FilingStatus, Frequency, Regulation
from app.models.filing import Filing
from app.models.obligation import ComplianceObligation, OrganizationObligation
from app.models.organization import Organization
from app.models.template import Template
from app.routers._helpers import changed_fields, paginate, record, snapshot
from app.schemas.common import MessageResponse, Page
from app.schemas.filing import (
    EventFilingRequest,
    FilingCreateRequest,
    FilingResponse,
    FilingSummary,
    FilingTransitionRequest,
    FilingTransitionResponse,
    FilingUpdateRequest,
)
from app.services import filing_generator, filing_workflow
from app.services.deadline_engine import DueDateRule, due_date_for, period_for, urgency

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/filings", tags=["filings"])

_AUDITED = (
    "status",
    "period_key",
    "due_date",
    "extended_due_date",
    "acknowledgement_no",
    "tax_payable_paise",
    "tax_paid_paise",
    "penalty_paise",
    "late_fee_paise",
    "template_id",
)


def _summary(filing: Filing, obligation: ComplianceObligation | None, today: date) -> FilingSummary:
    response = FilingSummary.model_validate(filing)
    response.effective_due_date = filing.effective_due_date
    response.days_until_due = filing.days_until(today)
    response.urgency = urgency(response.days_until_due)
    response.is_open = filing.is_open
    if obligation is not None:
        response.title = obligation.title
    return response


def _detail(filing: Filing, obligation: ComplianceObligation | None, today: date) -> FilingResponse:
    response = FilingResponse.model_validate(filing)
    response.effective_due_date = filing.effective_due_date
    response.days_until_due = filing.days_until(today)
    response.urgency = urgency(response.days_until_due)
    response.is_open = filing.is_open
    response.allowed_transitions = filing_workflow.allowed_from(filing.status)
    if obligation is not None:
        response.title = obligation.title
        response.obligation_code = obligation.code
        response.obligation_title = obligation.title
        response.penalty_description = obligation.penalty_description
    return response


def _get_filing(db: Session, filing_id: int, ctx: TenantContext) -> Filing:
    filing = db.execute(
        scoped(Filing, ctx).where(Filing.id == filing_id)
    ).scalar_one_or_none()
    if filing is None:
        raise NotFoundError("No such filing")
    return filing


def _obligation_for(db: Session, filing: Filing) -> ComplianceObligation | None:
    return db.get(ComplianceObligation, filing.obligation_id)


@router.get("", response_model=Page[FilingSummary], summary="List filings")
def list_filings(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    regulation: Regulation | None = None,
    status: FilingStatus | None = None,
    frequency: Frequency | None = None,
    period_key: str | None = Query(None, max_length=32),
    due_before: date | None = None,
    due_after: date | None = None,
    only_open: bool = Query(False, description="Only filings that still need work"),
    overdue_only: bool = Query(False),
    search: str | None = Query(None, max_length=128),
    today: date | None = Query(None),
):
    """This organization's filings, filtered and paginated."""
    reference = today or date.today()

    stmt = scoped(Filing, ctx).join(
        ComplianceObligation, ComplianceObligation.id == Filing.obligation_id
    )
    if regulation is not None:
        stmt = stmt.where(Filing.regulation == regulation)
    if status is not None:
        stmt = stmt.where(Filing.status == status)
    if frequency is not None:
        stmt = stmt.where(ComplianceObligation.frequency == frequency)
    if period_key:
        stmt = stmt.where(Filing.period_key == period_key)
    if due_before is not None:
        stmt = stmt.where(Filing.due_date <= due_before)
    if due_after is not None:
        stmt = stmt.where(Filing.due_date >= due_after)
    if only_open or overdue_only:
        stmt = stmt.where(
            Filing.status.notin_(
                [
                    FilingStatus.SUBMITTED,
                    FilingStatus.ACKNOWLEDGED,
                    FilingStatus.LATE_FILED,
                    FilingStatus.NOT_APPLICABLE,
                ]
            )
        )
    if overdue_only:
        # Against ``due_date`` rather than the effective date, because an
        # extension is stored separately and SQL cannot read the property.
        # The coalesce keeps an extended filing out of the overdue list, which
        # is the case that matters — an extension exists precisely to stop
        # something counting as late.
        stmt = stmt.where(
            or_(
                (Filing.extended_due_date.is_(None)) & (Filing.due_date < reference),
                (Filing.extended_due_date.is_not(None))
                & (Filing.extended_due_date < reference),
            )
        )
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                ComplianceObligation.title.ilike(pattern),
                Filing.filing_type.ilike(pattern),
                Filing.period_key.ilike(pattern),
                Filing.acknowledgement_no.ilike(pattern),
            )
        )

    stmt = stmt.order_by(Filing.due_date, Filing.id)
    rows, total = paginate(db, stmt, limit=limit, offset=offset)

    obligations = {
        o.id: o
        for o in db.execute(
            select(ComplianceObligation).where(
                ComplianceObligation.id.in_([f.obligation_id for f in rows])
            )
        ).scalars()
    }
    return Page[FilingSummary](
        items=[_summary(f, obligations.get(f.obligation_id), reference) for f in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{filing_id}", response_model=FilingResponse, summary="One filing")
def get_filing(
    filing_id: int,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    today: date | None = Query(None),
):
    filing = _get_filing(db, filing_id, ctx)
    return _detail(filing, _obligation_for(db, filing), today or date.today())


@router.post(
    "",
    response_model=FilingResponse,
    status_code=http_status.HTTP_201_CREATED,
    summary="Create a filing by hand",
)
def create_filing(
    payload: FilingCreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
):
    """Create a filing the generator would not have made.

    The due date is computed from the obligation's own rule when the caller
    does not supply one. Accepting a caller-supplied date without a fallback
    would be the easy version and the wrong one: the statutory date is the
    obligation's property, and a client that had to compute it would be a
    second implementation of :mod:`app.services.deadline_engine`.
    """
    obligation = db.execute(
        catalogue_scoped(ComplianceObligation, ctx).where(
            ComplianceObligation.id == payload.obligation_id
        )
    ).scalar_one_or_none()
    if obligation is None:
        raise NotFoundError("No such obligation")

    existing = db.execute(
        select(Filing).where(
            Filing.organization_id == ctx.org_id,
            Filing.obligation_id == obligation.id,
            Filing.period_key == payload.period_key,
            Filing.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(
            "A filing for that obligation and period already exists",
            details={"filing_id": existing.id, "period_key": payload.period_key},
        )

    due_date = payload.due_date
    if due_date is None:
        link = db.execute(
            select(OrganizationObligation).where(
                OrganizationObligation.organization_id == ctx.org_id,
                OrganizationObligation.obligation_id == obligation.id,
                OrganizationObligation.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        rule = DueDateRule.from_obligation(obligation, override=link)
        period = (
            period_for(rule.frequency, payload.period_start)
            if payload.period_start
            else None
        )
        if period is None:
            raise ValidationError(
                "Provide a due_date, or a period_start the due date can be derived from",
                details={"frequency": str(rule.frequency)},
            )
        due_date = due_date_for(rule, period)

    if payload.template_id is not None:
        _require_template(db, ctx, payload.template_id)

    filing = Filing(
        organization_id=ctx.org_id,
        obligation_id=obligation.id,
        regulation=obligation.regulation,
        filing_type=obligation.filing_type,
        period_key=payload.period_key,
        period_start=payload.period_start,
        period_end=payload.period_end,
        due_date=due_date,
        status=FilingStatus.NOT_STARTED,
        data_json=payload.data_json,
        template_id=payload.template_id,
        notes=payload.notes,
        prepared_by_id=ctx.user_id,
    )
    db.add(filing)
    db.flush()
    filing_workflow.ensure_deadline(db, filing)

    record(
        db,
        ctx,
        request,
        action=AuditAction.CREATE,
        entity_type="filing",
        entity_id=filing.id,
        after=snapshot(filing, _AUDITED),
        summary=f"{obligation.title} for {filing.period_key} created",
    )
    db.commit()
    db.refresh(filing)
    return _detail(filing, obligation, date.today())


def _require_template(db: Session, ctx: TenantContext, template_id: int) -> Template:
    template = db.execute(
        catalogue_scoped(Template, ctx).where(Template.id == template_id)
    ).scalar_one_or_none()
    if template is None:
        raise NotFoundError("No such template")
    return template


@router.patch("/{filing_id}", response_model=FilingResponse, summary="Edit a filing")
def update_filing(
    filing_id: int,
    payload: FilingUpdateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
):
    """Edit a filing's content.

    Refused once the filing has been submitted. At that point the numbers are
    with the regulator, and editing our copy to say something else would make
    the record diverge from what was actually filed — which is the one thing
    the audit trail exists to prevent. A correction after submission is a
    revised return: a new filing.
    """
    filing = _get_filing(db, filing_id, ctx)

    if filing.status in (
        FilingStatus.SUBMITTED,
        FilingStatus.LATE_FILED,
        FilingStatus.ACKNOWLEDGED,
    ):
        raise ConflictError(
            f"A filing that is {filing.status} can no longer be edited. "
            "File a revised return instead.",
            details={"status": str(filing.status)},
        )

    before = snapshot(filing, _AUDITED)
    changes = payload.model_dump(exclude_unset=True)

    if changes.get("template_id") is not None:
        _require_template(db, ctx, changes["template_id"])

    extended = changes.pop("extended_due_date", None)
    if extended is not None:
        if extended < filing.due_date:
            raise ValidationError(
                "An extension cannot be earlier than the original due date",
                details={"due_date": filing.due_date.isoformat()},
            )
        filing.extended_due_date = extended

    for field, value in changes.items():
        if value is not None:
            setattr(filing, field, value)

    # The deadline row follows the extension, or reminders would keep firing
    # against a date the regulator has moved.
    filing_workflow.ensure_deadline(db, filing)

    diff = changed_fields(before, snapshot(filing, _AUDITED))
    if diff or "data_json" in changes:
        record(
            db,
            ctx,
            request,
            action=AuditAction.UPDATE,
            entity_type="filing",
            entity_id=filing.id,
            before={k: v["from"] for k, v in diff.items()},
            after={k: v["to"] for k, v in diff.items()},
            summary=f"Filing {filing.period_key} updated",
        )
    db.commit()
    db.refresh(filing)
    return _detail(filing, _obligation_for(db, filing), date.today())


@router.post(
    "/{filing_id}/transition",
    response_model=FilingTransitionResponse,
    summary="Move a filing to another status",
)
def transition_filing(
    filing_id: int,
    payload: FilingTransitionRequest,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
    today: date | None = Query(None),
):
    """Advance a filing through its lifecycle.

    The role each target needs is enforced in
    :func:`app.services.filing_workflow.check_transition`, not by the
    dependency here — the floor for any write is Staff, and approving or
    submitting requires Compliance Manager, which is a per-target rule the
    dependency cannot express.
    """
    filing = _get_filing(db, filing_id, ctx)
    before = snapshot(filing, _AUDITED)

    outcome = filing_workflow.apply_transition(
        db,
        filing,
        payload.status,
        ctx,
        acknowledgement_no=payload.acknowledgement_no,
        rejection_reason=payload.rejection_reason,
        notes=payload.notes,
        today=today,
    )

    action = {
        FilingStatus.APPROVED: AuditAction.APPROVE,
        FilingStatus.REJECTED: AuditAction.REJECT,
        FilingStatus.SUBMITTED: AuditAction.SUBMIT,
        FilingStatus.LATE_FILED: AuditAction.SUBMIT,
    }.get(outcome.current, AuditAction.UPDATE)

    summary = f"Filing {filing.period_key} moved from {outcome.previous} to {outcome.current}"
    if outcome.was_late:
        summary += " (recorded as late: submitted after the due date)"

    record(
        db,
        ctx,
        request,
        action=action,
        entity_type="filing",
        entity_id=filing.id,
        before=before,
        after=snapshot(filing, _AUDITED),
        summary=summary,
    )
    db.commit()
    db.refresh(filing)

    return FilingTransitionResponse(
        filing=_detail(filing, _obligation_for(db, filing), today or date.today()),
        previous_status=outcome.previous,
        recorded_as_late=outcome.was_late,
    )


@router.post(
    "/events",
    response_model=FilingResponse,
    status_code=http_status.HTTP_201_CREATED,
    summary="Record an event that triggers a filing",
)
def create_event_filing(
    payload: EventFilingRequest,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
):
    """Record the event an event-based obligation hangs off.

    FC-GPR is due 30 days after an allotment. There is no allotment until
    someone records one, which is why the calendar sweep skips these entirely
    and this endpoint exists instead.

    Recording the same event twice is a 409 rather than a second filing — the
    period key for an event-based filing is the event's own date, so the
    uniqueness constraint catches it.
    """
    obligation = db.execute(
        catalogue_scoped(ComplianceObligation, ctx).where(
            ComplianceObligation.id == payload.obligation_id
        )
    ).scalar_one_or_none()
    if obligation is None:
        raise NotFoundError("No such obligation")
    if obligation.frequency not in (Frequency.EVENT_BASED, Frequency.ONE_TIME):
        raise ValidationError(
            f"{obligation.code} is a {obligation.frequency} obligation and is "
            "generated from the calendar, not from an event",
            details={"frequency": str(obligation.frequency)},
        )

    org = db.get(Organization, ctx.org_id)
    if org is None:
        raise NotFoundError("No such organization")

    filing = filing_generator.record_event_filing(
        db, org, obligation, event_date=payload.event_date, data=payload.data_json
    )
    if filing is None:
        raise ConflictError(
            "A filing for that obligation and event date already exists",
            details={"event_date": payload.event_date.isoformat()},
        )

    filing.prepared_by_id = ctx.user_id
    if payload.notes:
        filing.notes = payload.notes

    record(
        db,
        ctx,
        request,
        action=AuditAction.CREATE,
        entity_type="filing",
        entity_id=filing.id,
        after={
            **snapshot(filing, _AUDITED),
            "event_date": payload.event_date.isoformat(),
        },
        summary=(
            f"{obligation.title} triggered by an event on "
            f"{payload.event_date.isoformat()}; due {filing.due_date.isoformat()}"
        ),
    )
    db.commit()
    db.refresh(filing)
    return _detail(filing, obligation, date.today())


@router.delete(
    "/{filing_id}", response_model=MessageResponse, summary="Void a filing"
)
def delete_filing(
    filing_id: int,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Soft delete a filing.

    Refused for anything already submitted, for the same reason editing is:
    the record must continue to match what went to the regulator.

    The generator will not recreate it. :func:`_existing_period_keys` there
    counts soft-deleted rows deliberately, so voiding a filing is a decision
    that sticks rather than one the next nightly sweep undoes.
    """
    filing = _get_filing(db, filing_id, ctx)

    if filing.status in (
        FilingStatus.SUBMITTED,
        FilingStatus.LATE_FILED,
        FilingStatus.ACKNOWLEDGED,
    ):
        raise ConflictError(
            f"A filing that is {filing.status} cannot be voided",
            details={"status": str(filing.status)},
        )

    before = snapshot(filing, _AUDITED)
    filing.soft_delete()

    deadline = filing_workflow.ensure_deadline(db, filing)
    deadline.soft_delete()

    record(
        db,
        ctx,
        request,
        action=AuditAction.SOFT_DELETE,
        entity_type="filing",
        entity_id=filing.id,
        before=before,
        summary=f"Filing {filing.period_key} voided",
    )
    db.commit()
    return MessageResponse(message="Filing voided")
