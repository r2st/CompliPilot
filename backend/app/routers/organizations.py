"""The organization profile, and the applicability sync that hangs off it.

Every route here acts on ``ctx.org_id`` — the organization the request is
*for*, which for a CA firm member who has switched into a client is the
client's. That is deliberate and is the whole point of the delegation model: a
CA editing a client's turnover is editing the client's profile, and the audit
entry lands in both chains.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.crypto import decrypt, encrypt, fingerprint
from app.core.database import get_db
from app.core.deps import get_tenant_context, require_admin, require_manager
from app.core.errors import ConflictError, NotFoundError
from app.core.tenancy import TenantContext
from app.models.enums import AuditAction
from app.models.organization import Organization
from app.routers._helpers import changed_fields, record, snapshot
from app.schemas.obligation import ApplicabilitySyncResponse, CoverageResponse
from app.schemas.organization import (
    OrganizationProfileUpdate,
    OrganizationResponse,
)
from app.services import applicability

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/organizations", tags=["organizations"])

# The encrypted columns, and the fingerprint column each one is mirrored into.
# A pair rather than two lists so a new identifier cannot be added to one and
# forgotten in the other — which would leave a value encrypted but unfindable,
# and its uniqueness constraint enforcing nothing.
ENCRYPTED_IDENTIFIERS: tuple[tuple[str, str], ...] = (
    ("gstin", "gstin_fingerprint"),
    ("pan", "pan_fingerprint"),
    ("cin", "cin_fingerprint"),
)

# Stored in clear: neither is personal data, and both are published by their
# registries anyway.
PLAIN_IDENTIFIERS: tuple[str, ...] = ("llpin", "tan", "firm_registration_no")

# What an audit entry records about a profile change. Not every column — the
# trail is read by more people than the row, so the encrypted identifiers are
# recorded as *fingerprints* elsewhere and never as plaintext here.
_AUDITED = (
    "name",
    "legal_name",
    "entity_type",
    "state",
    "industry",
    "annual_turnover_paise",
    "employee_count",
    "incorporation_date",
    "financial_year_end_month",
    "is_listed",
    "has_foreign_investment",
    "handles_personal_data",
    "contact_email",
    "contact_phone",
    "plan_tier",
    "is_active",
)


def to_response(org: Organization) -> OrganizationResponse:
    """Serialize an organization, decrypting the identifiers on the way out.

    Decryption happens here rather than in the model so that the list
    endpoints, which use :class:`OrganizationSummary`, never pay for it — a CA
    firm's 400-client list would otherwise perform 1,200 AES operations to
    render names nobody asked for.
    """
    payload = {
        **{c.name: getattr(org, c.name) for c in org.__table__.columns},
        **{field: decrypt(getattr(org, field)) for field, _ in ENCRYPTED_IDENTIFIERS},
    }
    return OrganizationResponse.model_validate(payload)


def apply_identifier(
    db: Session, org: Organization, field: str, fingerprint_field: str, value: str
) -> None:
    """Set one encrypted identifier, refusing a duplicate.

    The uniqueness check is done here as well as by the partial unique index,
    because the index gives a 409 with no field name and the user needs to know
    *which* identifier collided. The index remains the real guarantee — this
    check races, and losing that race is what the index is for.
    """
    existing = db.query(Organization).filter(
        getattr(Organization, fingerprint_field) == fingerprint(value),
        Organization.id != org.id,
        Organization.deleted_at.is_(None),
    ).first()
    if existing is not None:
        raise ConflictError(
            f"That {field.upper()} is already registered to another organization",
            details={"field": field},
        )
    setattr(org, field, encrypt(value))
    setattr(org, fingerprint_field, fingerprint(value))


@router.get("/me", response_model=OrganizationResponse, summary="The organization in context")
def get_my_organization(
    ctx: TenantContext = Depends(get_tenant_context), db: Session = Depends(get_db)
):
    """The organization this request acts for.

    For a company user, their own. For a CA firm member who has switched into a
    client, the client's — which is what makes one profile screen work for both
    without the frontend knowing which case it is in.
    """
    org = db.get(Organization, ctx.org_id)
    if org is None or org.deleted_at is not None:
        raise NotFoundError("No such organization")
    return to_response(org)


@router.patch("/me", response_model=OrganizationResponse, summary="Update the profile")
def update_my_organization(
    payload: OrganizationProfileUpdate,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Edit the profile.

    Compliance Manager and above, because these fields drive applicability:
    changing turnover from ₹4 crore to ₹6 crore adds a tax audit and a GSTR-9C
    to the calendar, which is not a change a read-only or staff account should
    be able to make silently.
    """
    org = db.get(Organization, ctx.org_id)
    if org is None or org.deleted_at is not None:
        raise NotFoundError("No such organization")

    before = snapshot(org, _AUDITED)
    changes = payload.model_dump(exclude_unset=True)

    for field, fingerprint_field in ENCRYPTED_IDENTIFIERS:
        value = changes.pop(field, None)
        if value:
            apply_identifier(db, org, field, fingerprint_field, value)

    for field in PLAIN_IDENTIFIERS:
        value = changes.pop(field, None)
        if value:
            setattr(org, field, value)

    for field, value in changes.items():
        # ``exclude_unset`` already dropped absent keys, so a None that reaches
        # here was sent explicitly. The profile schema documents that None
        # means "leave alone" rather than "clear" — see its docstring — so it
        # is skipped rather than written.
        if value is not None:
            setattr(org, field, value)

    after = snapshot(org, _AUDITED)
    diff = changed_fields(before, after)
    if diff:
        record(
            db,
            ctx,
            request,
            action=AuditAction.UPDATE,
            entity_type="organization",
            entity_id=org.id,
            before={k: v["from"] for k, v in diff.items()},
            after={k: v["to"] for k, v in diff.items()},
            summary=f"Profile updated: {', '.join(sorted(diff))}",
        )
    db.commit()
    db.refresh(org)
    return to_response(org)


@router.post(
    "/me/applicability/sync",
    response_model=ApplicabilitySyncResponse,
    summary="Re-evaluate which obligations apply",
)
def sync_applicability(
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Run the applicability engine over the catalogue for this organization.

    Called automatically after a profile change would be tempting and is not
    done, because the engine's verdicts are shown to a human for confirmation:
    a sync triggered as a side effect of saving an address would surprise
    someone with twelve new obligations they did not ask to evaluate.

    Never overwrites a human override — see
    :func:`app.services.applicability.sync_organization_obligations`.
    """
    org = db.get(Organization, ctx.org_id)
    if org is None or org.deleted_at is not None:
        raise NotFoundError("No such organization")

    result = applicability.sync_organization_obligations(db, org)
    record(
        db,
        ctx,
        request,
        action=AuditAction.UPDATE,
        entity_type="organization_obligations",
        entity_id=org.id,
        after=result.as_dict(),
        summary=(
            f"Applicability re-evaluated: {result.evaluated} obligations, "
            f"{result.created} new, {result.verdict_changed} changed"
        ),
    )
    db.commit()
    return ApplicabilitySyncResponse(**result.as_dict())


@router.get(
    "/me/coverage",
    response_model=CoverageResponse,
    summary="Applicable obligations per regulation",
)
def coverage(
    ctx: TenantContext = Depends(get_tenant_context), db: Session = Depends(get_db)
):
    """The coverage strip on the profile page."""
    by_regulation = applicability.coverage_by_regulation(db, ctx.org_id)
    total_applicable = sum(b["applicable"] for b in by_regulation.values())
    total_evaluated = sum(
        b["applicable"] + b["not_applicable"] for b in by_regulation.values()
    )
    return CoverageResponse(
        by_regulation=by_regulation,
        total_applicable=total_applicable,
        total_evaluated=total_evaluated,
    )


@router.delete(
    "/me",
    response_model=OrganizationResponse,
    summary="Deactivate this organization",
)
def deactivate_organization(
    request: Request,
    ctx: TenantContext = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Soft delete the organization and mark it inactive.

    Not a hard delete, and never will be: the audit trail is append-only by
    contract, filings are evidence at an assessment years later, and a
    ``RESTRICT`` foreign key on every tenant table means the database would
    refuse anyway. What this does is stop the organization serving traffic —
    :func:`app.core.deps.get_current_organization` rejects an inactive one on
    the next request, including this user's own.
    """
    org = db.get(Organization, ctx.org_id)
    if org is None or org.deleted_at is not None:
        raise NotFoundError("No such organization")

    before = snapshot(org, _AUDITED)
    org.is_active = False
    org.soft_delete()

    record(
        db,
        ctx,
        request,
        action=AuditAction.SOFT_DELETE,
        entity_type="organization",
        entity_id=org.id,
        before=before,
        after=snapshot(org, _AUDITED),
        summary=f"Organization {org.name} deactivated",
    )
    db.commit()
    db.refresh(org)
    return to_response(org)
