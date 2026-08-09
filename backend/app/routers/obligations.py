"""Browsing the obligations catalogue, and recording what applies to whom.

Two resources that read as one. ``/obligations`` is the catalogue — the ~200
curated rules plus whatever the tenant has added. ``/obligations/mine`` is the
subset this organization owes, with the engine's verdict and any human override.

The catalogue is shared, so every read here goes through
:func:`app.core.tenancy.catalogue_scoped` rather than ``scoped``: a tenant sees
system rows (``organization_id IS NULL``) union their own, and never another
tenant's.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_tenant_context, require_manager
from app.core.errors import ConflictError, NotFoundError
from app.core.tenancy import TenantContext, catalogue_scoped
from app.models.enums import AuditAction, Frequency, Regulation
from app.models.obligation import ComplianceObligation, OrganizationObligation
from app.models.user import User
from app.routers._helpers import changed_fields, deny_system_row, paginate, record, snapshot
from app.schemas.common import MessageResponse, Page
from app.schemas.obligation import (
    ObligationCreateRequest,
    ObligationResponse,
    ObligationUpdateRequest,
    OrganizationObligationResponse,
    OrganizationObligationUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/obligations", tags=["obligations"])

_AUDITED = (
    "code",
    "title",
    "regulation",
    "frequency",
    "due_day",
    "due_month",
    "offset_days",
    "is_active",
)

_LINK_AUDITED = (
    "is_applicable_override",
    "engine_verdict",
    "frequency_override",
    "due_day_override",
    "owner_user_id",
)


@router.get("", response_model=Page[ObligationResponse], summary="Browse the catalogue")
def list_obligations(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    regulation: Regulation | None = None,
    frequency: Frequency | None = None,
    search: str | None = Query(None, max_length=128),
    include_inactive: bool = False,
    mine_only: bool = Query(False, description="Only this organization's own entries"),
):
    """The catalogue as this tenant sees it."""
    stmt = catalogue_scoped(ComplianceObligation, ctx)
    if not include_inactive:
        stmt = stmt.where(ComplianceObligation.is_active.is_(True))
    if regulation is not None:
        stmt = stmt.where(ComplianceObligation.regulation == regulation)
    if frequency is not None:
        stmt = stmt.where(ComplianceObligation.frequency == frequency)
    if mine_only:
        stmt = stmt.where(ComplianceObligation.organization_id == ctx.org_id)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                ComplianceObligation.title.ilike(pattern),
                ComplianceObligation.code.ilike(pattern),
                ComplianceObligation.filing_type.ilike(pattern),
            )
        )

    stmt = stmt.order_by(ComplianceObligation.regulation, ComplianceObligation.code)
    rows, total = paginate(db, stmt, limit=limit, offset=offset)
    return Page[ObligationResponse](
        items=[ObligationResponse.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/summary",
    response_model=dict,
    summary="Catalogue counts per regulation",
)
def catalogue_summary(
    ctx: TenantContext = Depends(get_tenant_context), db: Session = Depends(get_db)
):
    """How many active obligations exist per regulation, for the browse filters."""
    rows = db.execute(
        select(ComplianceObligation.regulation, func.count())
        .where(
            or_(
                ComplianceObligation.organization_id.is_(None),
                ComplianceObligation.organization_id == ctx.org_id,
            ),
            ComplianceObligation.deleted_at.is_(None),
            ComplianceObligation.is_active.is_(True),
        )
        .group_by(ComplianceObligation.regulation)
    ).all()
    by_regulation = {str(r[0]): int(r[1]) for r in rows}
    return {"by_regulation": by_regulation, "total": sum(by_regulation.values())}


@router.get(
    "/mine",
    response_model=Page[OrganizationObligationResponse],
    summary="What this organization owes",
)
def list_my_obligations(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    regulation: Regulation | None = None,
    applicable_only: bool = Query(
        False, description="Only the ones that actually apply"
    ),
    overridden_only: bool = Query(
        False, description="Only the ones a human has ruled on"
    ),
):
    """The organization's obligations, with the engine's verdict and any override.

    The negatives are returned too, unless ``applicable_only`` is set. "GSTR-9
    does not apply to you, because your turnover is under ₹2 crore" is a thing
    a CA has to be able to show a client, and a list of only the positives
    could not answer it.
    """
    stmt = (
        select(OrganizationObligation)
        .join(
            ComplianceObligation,
            ComplianceObligation.id == OrganizationObligation.obligation_id,
        )
        .where(
            OrganizationObligation.organization_id == ctx.org_id,
            OrganizationObligation.deleted_at.is_(None),
            ComplianceObligation.deleted_at.is_(None),
        )
    )
    if regulation is not None:
        stmt = stmt.where(ComplianceObligation.regulation == regulation)
    if overridden_only:
        stmt = stmt.where(OrganizationObligation.is_applicable_override.is_not(None))
    if applicable_only:
        # The effective verdict is the override when there is one and the
        # engine's otherwise. Expressed in SQL rather than filtered in Python
        # so the pagination count is right — filtering after the LIMIT would
        # return short pages and a total nobody could reconcile.
        stmt = stmt.where(
            or_(
                OrganizationObligation.is_applicable_override.is_(True),
                (OrganizationObligation.is_applicable_override.is_(None))
                & (OrganizationObligation.engine_verdict.is_(True)),
            )
        )

    stmt = stmt.order_by(ComplianceObligation.regulation, ComplianceObligation.code)
    rows, total = paginate(db, stmt, limit=limit, offset=offset)

    items = []
    for link in rows:
        response = OrganizationObligationResponse.model_validate(link)
        response.is_applicable = link.is_applicable
        if link.obligation is not None:
            response.obligation = ObligationResponse.model_validate(link.obligation)
        items.append(response)

    return Page[OrganizationObligationResponse](
        items=items, total=total, limit=limit, offset=offset
    )


@router.get("/{obligation_id}", response_model=ObligationResponse, summary="One obligation")
def get_obligation(
    obligation_id: int,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    row = db.execute(
        catalogue_scoped(ComplianceObligation, ctx).where(
            ComplianceObligation.id == obligation_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("No such obligation")
    return ObligationResponse.model_validate(row)


@router.post(
    "",
    response_model=ObligationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add an obligation of your own",
)
def create_obligation(
    payload: ObligationCreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """A tenant's own obligation, alongside the system catalogue.

    The code may collide with a system one — that is the override case, and the
    partial unique index is on ``(organization_id, code)`` precisely so it is
    allowed. What is refused is a second obligation with the same code *within
    this tenant*.
    """
    existing = db.execute(
        select(ComplianceObligation).where(
            ComplianceObligation.organization_id == ctx.org_id,
            ComplianceObligation.code == payload.code,
            ComplianceObligation.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(
            "You already have an obligation with that code",
            details={"code": payload.code, "obligation_id": existing.id},
        )

    obligation = ComplianceObligation(
        organization_id=ctx.org_id,
        is_system=False,
        is_active=True,
        **payload.model_dump(),
    )
    db.add(obligation)
    db.flush()

    record(
        db,
        ctx,
        request,
        action=AuditAction.CREATE,
        entity_type="compliance_obligation",
        entity_id=obligation.id,
        after=snapshot(obligation, _AUDITED),
        summary=f"Custom obligation {obligation.code} created",
    )
    db.commit()
    db.refresh(obligation)
    return ObligationResponse.model_validate(obligation)


@router.patch(
    "/{obligation_id}", response_model=ObligationResponse, summary="Edit your obligation"
)
def update_obligation(
    obligation_id: int,
    payload: ObligationUpdateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    obligation = db.execute(
        catalogue_scoped(ComplianceObligation, ctx).where(
            ComplianceObligation.id == obligation_id
        )
    ).scalar_one_or_none()
    if obligation is None:
        raise NotFoundError("No such obligation")
    deny_system_row(obligation, label="obligation")

    before = snapshot(obligation, _AUDITED)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(obligation, field, value)

    diff = changed_fields(before, snapshot(obligation, _AUDITED))
    if diff:
        record(
            db,
            ctx,
            request,
            action=AuditAction.UPDATE,
            entity_type="compliance_obligation",
            entity_id=obligation.id,
            before={k: v["from"] for k, v in diff.items()},
            after={k: v["to"] for k, v in diff.items()},
            summary=f"Obligation {obligation.code} updated",
        )
    db.commit()
    db.refresh(obligation)
    return ObligationResponse.model_validate(obligation)


@router.delete(
    "/{obligation_id}", response_model=MessageResponse, summary="Retire your obligation"
)
def delete_obligation(
    obligation_id: int,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Soft delete a tenant obligation, and the organization links to it.

    Filings already generated against it are left alone. They still have to
    resolve their ``obligation_id`` at an assessment, and a foreign key with
    ``ondelete=RESTRICT`` means the database would refuse to remove the row
    anyway — which is the intended outcome, not an obstacle.
    """
    obligation = db.execute(
        catalogue_scoped(ComplianceObligation, ctx).where(
            ComplianceObligation.id == obligation_id
        )
    ).scalar_one_or_none()
    if obligation is None:
        raise NotFoundError("No such obligation")
    deny_system_row(obligation, label="obligation")

    # Snapshot before the mutation. Taken afterwards it would record
    # ``is_active: False`` as the prior state, describing a row that was
    # already retired rather than the retirement being recorded.
    before = snapshot(obligation, _AUDITED)

    obligation.is_active = False
    obligation.soft_delete()
    for link in db.execute(
        select(OrganizationObligation).where(
            OrganizationObligation.obligation_id == obligation.id,
            OrganizationObligation.deleted_at.is_(None),
        )
    ).scalars():
        link.soft_delete()

    record(
        db,
        ctx,
        request,
        action=AuditAction.SOFT_DELETE,
        entity_type="compliance_obligation",
        entity_id=obligation.id,
        before=before,
        summary=f"Obligation {obligation.code} retired",
    )
    db.commit()
    return MessageResponse(message="Obligation retired")


@router.patch(
    "/mine/{link_id}",
    response_model=OrganizationObligationResponse,
    summary="Override whether an obligation applies",
)
def update_my_obligation(
    link_id: int,
    payload: OrganizationObligationUpdateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Record a human decision about one obligation.

    This is the override the applicability engine will never touch. A CA
    marking a client exempt has almost certainly seen a fact the profile does
    not carry, and the nightly sync is written to leave it alone — see
    :func:`app.services.applicability.sync_organization_obligations`.

    ``clear_override`` hands the question back to the engine. It is a separate
    flag because Pydantic cannot distinguish "key absent" from "key sent as
    null", and those mean opposite things here.
    """
    link = db.execute(
        select(OrganizationObligation).where(
            OrganizationObligation.id == link_id,
            OrganizationObligation.organization_id == ctx.org_id,
            OrganizationObligation.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if link is None:
        raise NotFoundError("No such obligation for this organization")

    before = snapshot(link, _LINK_AUDITED)
    changes = payload.model_dump(exclude_unset=True)
    clear = changes.pop("clear_override", False)

    if clear:
        link.is_applicable_override = None
    elif "is_applicable_override" in changes:
        link.is_applicable_override = changes["is_applicable_override"]
    changes.pop("is_applicable_override", None)

    if "owner_user_id" in changes:
        owner_id = changes.pop("owner_user_id")
        if owner_id is not None:
            owner = db.execute(
                select(User).where(
                    User.id == owner_id,
                    # The owner must belong to the organization that owes the
                    # obligation, or to the firm acting for it. Anything else
                    # would put a stranger's name on a client's compliance
                    # calendar.
                    User.organization_id.in_({ctx.org_id, ctx.home_org_id}),
                    User.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if owner is None:
                raise NotFoundError("No such user")
        link.owner_user_id = owner_id

    for field, value in changes.items():
        setattr(link, field, value)

    diff = changed_fields(before, snapshot(link, _LINK_AUDITED))
    if diff:
        record(
            db,
            ctx,
            request,
            action=AuditAction.UPDATE,
            entity_type="organization_obligation",
            entity_id=link.id,
            before={k: v["from"] for k, v in diff.items()},
            after={k: v["to"] for k, v in diff.items()},
            summary=(
                f"Applicability override for obligation {link.obligation_id}: "
                f"{', '.join(sorted(diff))}"
            ),
        )
    db.commit()
    db.refresh(link)

    response = OrganizationObligationResponse.model_validate(link)
    response.is_applicable = link.is_applicable
    if link.obligation is not None:
        response.obligation = ObligationResponse.model_validate(link.obligation)
    return response
