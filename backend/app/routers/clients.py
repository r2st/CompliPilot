"""CA firm → client engagements, and the staff assignments that gate access.

Everything here is scoped by ``ca_firm_id``, not by ``ctx.org_id``. An
engagement spans two organizations and the firm is the owner — see
:class:`app.models.organization.Client`. That means these routes are the one
place in the API that deliberately does *not* use
:func:`app.core.tenancy.scoped`, and each query says so.

Only a firm may call any of this. A company user has no clients, and answering
them with an empty list rather than a 403 would suggest the feature is
available to them.
"""
from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_tenant_context, require_admin, require_manager
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.tenancy import TenantContext
from app.models.enums import (
    AuditAction,
    EngagementStatus,
    FilingStatus,
    OrgType,
    UserRole,
    role_rank,
)
from app.models.filing import Filing
from app.models.organization import Client, Organization
from app.models.user import ClientAssignment, User
from app.routers._helpers import changed_fields, record, snapshot
from app.routers.organizations import ENCRYPTED_IDENTIFIERS, apply_identifier
from app.schemas.common import MessageResponse, Page
from app.schemas.organization import (
    ClientAssignmentRequest,
    ClientAssignmentResponse,
    ClientCreateRequest,
    ClientResponse,
    ClientUpdateRequest,
    OrganizationSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clients", tags=["clients"])

_AUDITED = (
    "engagement_type",
    "status",
    "start_date",
    "end_date",
    "assigned_user_id",
    "retainer_paise",
)

# Statuses that mean the filing still needs someone's attention.
_OPEN_STATUSES = (
    FilingStatus.NOT_STARTED,
    FilingStatus.DRAFT,
    FilingStatus.IN_REVIEW,
    FilingStatus.APPROVED,
    FilingStatus.REJECTED,
)


def require_firm(ctx: TenantContext, db: Session) -> Organization:
    """The caller's own organization, which must be a CA firm.

    Reads ``home_org_id``, not ``org_id``: a firm member who has switched into
    a client is still acting from their firm, and their client list is the
    firm's. Using ``org_id`` here would empty the list the moment someone
    switched context, which is exactly when they want to switch again.
    """
    firm = db.get(Organization, ctx.home_org_id)
    if firm is None or firm.deleted_at is not None:
        raise NotFoundError("No such organization")
    if firm.type != OrgType.CA_FIRM:
        raise ForbiddenError("Only a CA firm manages client engagements")
    return firm


def _visible_engagements(ctx: TenantContext, firm_id: int):
    """Engagements this user may see.

    Admins and Compliance Managers see the firm's whole book. Staff and
    Read-Only see only what :class:`ClientAssignment` grants them — section
    4.5's "staff can only see assigned clients", enforced on the list as well
    as on the individual read, because a list that leaked names would be the
    same disclosure one page earlier.
    """
    stmt = select(Client).where(
        Client.ca_firm_id == firm_id, Client.deleted_at.is_(None)
    )
    if role_rank(ctx.role) >= role_rank(UserRole.COMPLIANCE_MANAGER):
        return stmt
    return stmt.join(
        ClientAssignment,
        (ClientAssignment.client_id == Client.id)
        & (ClientAssignment.user_id == ctx.user_id)
        & (ClientAssignment.deleted_at.is_(None)),
    )


def _filing_counts(db: Session, org_ids: list[int], today: date) -> dict[int, tuple[int, int]]:
    """``{org_id: (open, overdue)}`` for a set of client organizations.

    One grouped query for the whole page rather than two per row. A firm with
    200 clients would otherwise fire 400 queries to render one list, which is
    the difference between a list that loads and one that times out.
    """
    if not org_ids:
        return {}

    rows = db.execute(
        select(
            Filing.organization_id,
            func.count().label("open_count"),
            func.sum(
                # A CASE rather than casting the boolean, because SQLite has no
                # boolean type to cast and would return the string 'true'.
                # ``coalesce`` because ``extended_due_date`` is usually null,
                # and a comparison against null is null rather than false.
                case(
                    (
                        func.coalesce(Filing.extended_due_date, Filing.due_date) < today,
                        1,
                    ),
                    else_=0,
                )
            ).label("overdue_count"),
        )
        .where(
            Filing.organization_id.in_(org_ids),
            Filing.deleted_at.is_(None),
            Filing.status.in_(_OPEN_STATUSES),
        )
        .group_by(Filing.organization_id)
    ).all()

    return {int(r[0]): (int(r[1] or 0), int(r[2] or 0)) for r in rows}


def _to_response(
    engagement: Client,
    org: Organization | None,
    counts: dict[int, tuple[int, int]] | None = None,
) -> ClientResponse:
    response = ClientResponse.model_validate(engagement)
    if org is not None:
        response.client_organization = OrganizationSummary.model_validate(org)
    if counts is not None:
        open_count, overdue = counts.get(engagement.client_org_id, (0, 0))
        response.open_filings = open_count
        response.overdue_filings = overdue
    return response


@router.get("", response_model=Page[ClientResponse], summary="List client engagements")
def list_clients(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: EngagementStatus | None = Query(None, alias="status"),
    search: str | None = Query(None, max_length=128),
    with_counts: bool = Query(
        True, description="Include open and overdue filing counts per client"
    ),
):
    """The firm's client book, paginated."""
    firm = require_firm(ctx, db)

    stmt = _visible_engagements(ctx, firm.id).join(
        Organization, Organization.id == Client.client_org_id
    )
    if status_filter is not None:
        stmt = stmt.where(Client.status == str(status_filter))
    if search:
        stmt = stmt.where(Organization.name.ilike(f"%{search}%"))

    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()
    engagements = list(
        db.execute(
            stmt.order_by(Organization.name).limit(limit).offset(offset)
        ).scalars().all()
    )

    orgs = {
        org.id: org
        for org in db.execute(
            select(Organization).where(
                Organization.id.in_([e.client_org_id for e in engagements])
            )
        ).scalars()
    }
    counts = (
        _filing_counts(db, [e.client_org_id for e in engagements], date.today())
        if with_counts
        else None
    )

    return Page[ClientResponse](
        items=[_to_response(e, orgs.get(e.client_org_id), counts) for e in engagements],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=ClientResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Engage a client",
)
def create_client(
    payload: ClientCreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Take on a client — either an organization already on the platform, or a new one.

    Creating the client's organization row here rather than reusing the firm's
    is what makes the data portable: when the engagement ends, the company's
    filings, documents and audit trail stay with the company and another firm
    can be engaged against the same organization.
    """
    firm = require_firm(ctx, db)

    if (payload.client_org_id is None) == (payload.organization is None):
        raise ValidationError(
            "Provide either client_org_id for an existing organization, or "
            "organization to create a new one — not both, and not neither"
        )

    if payload.client_org_id is not None:
        client_org = db.get(Organization, payload.client_org_id)
        if client_org is None or client_org.deleted_at is not None:
            raise NotFoundError("No such organization")
        if client_org.id == firm.id:
            raise ValidationError("A firm cannot engage itself as a client")
    else:
        spec = payload.organization
        assert spec is not None  # narrowed by the check above
        fields = spec.model_dump(exclude={"gstin", "pan", "cin", "llpin", "tan"})
        client_org = Organization(type=OrgType.COMPANY, **fields)
        db.add(client_org)
        db.flush()
        for field, fingerprint_field in ENCRYPTED_IDENTIFIERS:
            value = getattr(spec, field, None)
            if value:
                apply_identifier(db, client_org, field, fingerprint_field, value)
        for field in ("llpin", "tan"):
            value = getattr(spec, field, None)
            if value:
                setattr(client_org, field, value)

    existing = db.execute(
        select(Client).where(
            Client.ca_firm_id == firm.id,
            Client.client_org_id == client_org.id,
            Client.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(
            "This firm already has an engagement with that organization",
            details={"client_id": existing.id},
        )

    if payload.assigned_user_id is not None:
        _require_firm_member(db, firm.id, payload.assigned_user_id)

    engagement = Client(
        ca_firm_id=firm.id,
        client_org_id=client_org.id,
        engagement_type=str(payload.engagement_type),
        status=str(EngagementStatus.ACTIVE),
        start_date=payload.start_date or date.today(),
        end_date=payload.end_date,
        assigned_user_id=payload.assigned_user_id,
        retainer_paise=payload.retainer_paise,
        notes=payload.notes,
    )
    db.add(engagement)
    db.flush()

    # Recorded in the firm's chain. Not in the client's: from the client
    # organization's point of view nothing happened to their data yet, and
    # opening their trail with an entry they did not cause would be noise.
    record(
        db,
        ctx,
        request,
        action=AuditAction.CREATE,
        entity_type="client",
        entity_id=engagement.id,
        after={
            "client_org_id": client_org.id,
            "name": client_org.name,
            "engagement_type": engagement.engagement_type,
        },
        summary=f"Engaged {client_org.name}",
    )
    db.commit()
    db.refresh(engagement)
    return _to_response(engagement, client_org)


def _require_firm_member(db: Session, firm_id: int, user_id: int) -> User:
    """The named user must work at this firm.

    Checked because ``assigned_user_id`` is a plain FK to ``users`` with no
    tenant constraint of its own — without this, a firm could assign a client
    to a user id belonging to a different firm and that user would appear in
    the workload report of an organization they have never heard of.
    """
    user = db.execute(
        select(User).where(
            User.id == user_id,
            User.organization_id == firm_id,
            User.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("No such user in this firm")
    return user


def _get_engagement(db: Session, ctx: TenantContext, firm_id: int, client_id: int) -> Client:
    engagement = db.execute(
        _visible_engagements(db, ctx, firm_id).where(Client.id == client_id)
    ).scalar_one_or_none()
    if engagement is None:
        raise NotFoundError("No such client engagement")
    return engagement


@router.get("/{client_id}", response_model=ClientResponse, summary="One engagement")
def get_client(
    client_id: int,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    firm = require_firm(ctx, db)
    engagement = _get_engagement(db, ctx, firm.id, client_id)
    org = db.get(Organization, engagement.client_org_id)
    counts = _filing_counts(db, [engagement.client_org_id], date.today())
    return _to_response(engagement, org, counts)


@router.patch("/{client_id}", response_model=ClientResponse, summary="Update an engagement")
def update_client(
    client_id: int,
    payload: ClientUpdateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Change the terms of an engagement, or pause and terminate it.

    Terminating is the interesting one: it takes effect on the next request
    because :func:`app.core.tenancy.resolve_delegation` re-checks the
    engagement's status every time rather than trusting the token, so an
    outstanding twelve-hour token stops reaching the client immediately.
    """
    firm = require_firm(ctx, db)
    engagement = _get_engagement(db, ctx, firm.id, client_id)
    before = snapshot(engagement, _AUDITED)

    changes = payload.model_dump(exclude_unset=True)
    if "assigned_user_id" in changes and changes["assigned_user_id"] is not None:
        _require_firm_member(db, firm.id, changes["assigned_user_id"])

    for field, value in changes.items():
        if field in ("engagement_type", "status") and value is not None:
            setattr(engagement, field, str(value))
        elif field == "assigned_user_id":
            # Explicit null clears the assignment, which is how a client is
            # returned to the unassigned queue.
            engagement.assigned_user_id = value
        elif value is not None:
            setattr(engagement, field, value)

    if engagement.status == str(EngagementStatus.TERMINATED) and engagement.end_date is None:
        engagement.end_date = date.today()

    after = snapshot(engagement, _AUDITED)
    diff = changed_fields(before, after)
    if diff:
        record(
            db,
            ctx,
            request,
            action=AuditAction.UPDATE,
            entity_type="client",
            entity_id=engagement.id,
            before={k: v["from"] for k, v in diff.items()},
            after={k: v["to"] for k, v in diff.items()},
            summary=f"Engagement updated: {', '.join(sorted(diff))}",
        )
    db.commit()
    db.refresh(engagement)
    return _to_response(engagement, db.get(Organization, engagement.client_org_id))


@router.delete(
    "/{client_id}", response_model=MessageResponse, summary="End an engagement"
)
def delete_client(
    client_id: int,
    request: Request,
    ctx: TenantContext = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Soft delete the engagement.

    The client's organization and all of its data are left untouched. That is
    the point: offboarding a client must not destroy the filings they may need
    at an assessment, and the organization stays available for another firm to
    engage.
    """
    firm = require_firm(ctx, db)
    engagement = _get_engagement(db, ctx, firm.id, client_id)

    engagement.status = str(EngagementStatus.TERMINATED)
    engagement.end_date = engagement.end_date or date.today()
    engagement.soft_delete()

    # The assignments go with it, or a staff member would keep a live grant to
    # a client the firm no longer acts for.
    for assignment in db.execute(
        select(ClientAssignment).where(
            ClientAssignment.client_id == engagement.id,
            ClientAssignment.deleted_at.is_(None),
        )
    ).scalars():
        assignment.soft_delete()

    record(
        db,
        ctx,
        request,
        action=AuditAction.SOFT_DELETE,
        entity_type="client",
        entity_id=engagement.id,
        before=snapshot(engagement, _AUDITED),
        summary=f"Engagement with organization {engagement.client_org_id} ended",
    )
    db.commit()
    return MessageResponse(message="Engagement ended")


# --------------------------------------------------------------------------
# Staff assignments
# --------------------------------------------------------------------------


@router.get(
    "/{client_id}/assignments",
    response_model=list[ClientAssignmentResponse],
    summary="Who may act for this client",
)
def list_assignments(
    client_id: int,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    firm = require_firm(ctx, db)
    engagement = _get_engagement(db, ctx, firm.id, client_id)
    rows = db.execute(
        select(ClientAssignment).where(
            ClientAssignment.client_id == engagement.id,
            ClientAssignment.deleted_at.is_(None),
        )
    ).scalars()
    return [ClientAssignmentResponse.model_validate(r) for r in rows]


@router.post(
    "/{client_id}/assignments",
    response_model=ClientAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Assign a staff member to this client",
)
def create_assignment(
    client_id: int,
    payload: ClientAssignmentRequest,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Grant one staff member access to one client.

    ``granted_role`` can only narrow. A Staff user handed an ``admin`` grant
    would otherwise hold rights on the client they do not hold at their own
    firm — the cap is applied in
    :func:`app.core.tenancy.resolve_delegation`, and rejected here as well so
    the mistake is reported rather than silently ignored at request time.
    """
    firm = require_firm(ctx, db)
    engagement = _get_engagement(db, ctx, firm.id, client_id)
    user = _require_firm_member(db, firm.id, payload.user_id)

    granted: UserRole | None = None
    if payload.granted_role is not None:
        try:
            granted = UserRole(payload.granted_role)
        except ValueError as exc:
            raise ValidationError(f"Unknown role {payload.granted_role!r}") from exc
        if role_rank(granted) > role_rank(user.role):
            raise ValidationError(
                "An assignment can only narrow a user's access, never widen it",
                details={"user_role": str(user.role), "granted_role": str(granted)},
            )

    existing = db.execute(
        select(ClientAssignment).where(
            ClientAssignment.user_id == user.id,
            ClientAssignment.client_id == engagement.id,
            ClientAssignment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.granted_role = granted
        db.commit()
        db.refresh(existing)
        return ClientAssignmentResponse.model_validate(existing)

    assignment = ClientAssignment(
        user_id=user.id,
        client_id=engagement.id,
        client_org_id=engagement.client_org_id,
        granted_role=granted,
    )
    db.add(assignment)
    db.flush()

    record(
        db,
        ctx,
        request,
        action=AuditAction.ROLE_CHANGE,
        entity_type="client_assignment",
        entity_id=assignment.id,
        after={
            "user_id": user.id,
            "client_id": engagement.id,
            "granted_role": str(granted) if granted else None,
        },
        summary=f"{user.full_name} assigned to client organization {engagement.client_org_id}",
    )
    db.commit()
    db.refresh(assignment)
    return ClientAssignmentResponse.model_validate(assignment)


@router.delete(
    "/{client_id}/assignments/{user_id}",
    response_model=MessageResponse,
    summary="Revoke a staff assignment",
)
def delete_assignment(
    client_id: int,
    user_id: int,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    firm = require_firm(ctx, db)
    engagement = _get_engagement(db, ctx, firm.id, client_id)

    assignment = db.execute(
        select(ClientAssignment).where(
            ClientAssignment.user_id == user_id,
            ClientAssignment.client_id == engagement.id,
            ClientAssignment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise NotFoundError("No such assignment")

    assignment.soft_delete()
    record(
        db,
        ctx,
        request,
        action=AuditAction.ROLE_CHANGE,
        entity_type="client_assignment",
        entity_id=assignment.id,
        before={"user_id": user_id, "client_id": engagement.id},
        summary=f"User {user_id} unassigned from client organization {engagement.client_org_id}",
    )
    db.commit()
    return MessageResponse(message="Assignment revoked")
