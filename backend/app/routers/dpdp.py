"""The DPDP Act toolkit (section 4.4): consent, data map, breaches, requests, PIAs.

Five resources under one prefix, because a Data Protection Board inquiry asks
about all five at once and the product should be shaped like the question.

**The data principal's identifier never appears in a list response.** It is the
most sensitive column in the product — an email or phone number belonging to a
member of the public rather than to a customer of ours. It is stored encrypted
with a keyed fingerprint beside it, decrypted only on a single-record read, and
looked up by fingerprint. A consent register that decrypted 40,000 addresses to
render a page would itself be the reportable incident.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.crypto import decrypt, encrypt, fingerprint
from app.core.database import get_db
from app.core.deps import get_tenant_context, require_manager, require_writer
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.tenancy import TenantContext, scoped
from app.models.dpdp import (
    BREACH_NOTIFICATION_HOURS,
    DSR_SLA_DAYS,
    BreachIncident,
    ConsentRecord,
    DataMapEntry,
    DataSubjectRequest,
    PrivacyImpactAssessment,
)
from app.models.enums import AuditAction
from app.models.mixins import utcnow
from app.routers._helpers import paginate, record, snapshot
from app.schemas.common import MessageResponse, Page
from app.schemas.dpdp import (
    BreachCreateRequest,
    BreachIncidentResponse,
    BreachNotifyRequest,
    BreachUpdateRequest,
    ConsentCreateRequest,
    ConsentRecordResponse,
    ConsentWithdrawRequest,
    DataMapEntryRequest,
    DataMapEntryResponse,
    DataSubjectRequestCreate,
    DataSubjectRequestResponse,
    DataSubjectRequestUpdate,
    DPDPReadinessResponse,
    PIARequest,
    PIAResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dpdp", tags=["dpdp"])


def _next_reference(db: Session, model, org_id: int, prefix: str) -> str:
    """A per-organization, human-quotable reference: ``BR-2026-0007``.

    Sequential per organization rather than global, so a client cannot infer
    how many breaches other tenants have reported from the number on their own.
    Derived from a COUNT, which can collide under concurrency — the partial
    unique index on ``(organization_id, reference)`` is what actually
    guarantees uniqueness, and a collision surfaces as a 409 the caller retries.
    """
    year = date.today().year
    count = db.execute(
        select(func.count()).select_from(model).where(model.organization_id == org_id)
    ).scalar_one()
    return f"{prefix}-{year}-{int(count) + 1:04d}"


# --------------------------------------------------------------------------
# Consent register
# --------------------------------------------------------------------------


@router.get(
    "/consents", response_model=Page[ConsentRecordResponse], summary="The consent register"
)
def list_consents(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    purpose: str | None = Query(None, max_length=255),
    granted_only: bool = Query(False),
    principal_ref: str | None = Query(
        None, description="Find one principal's records; matched by keyed fingerprint"
    ),
):
    """The register, without any principal identifiers.

    ``principal_ref`` searches by fingerprinting the supplied value and
    matching that — the plaintext never goes near a WHERE clause, and the
    column it matches is indexed, so this is a lookup rather than a scan.
    """
    stmt = scoped(ConsentRecord, ctx)
    if purpose:
        stmt = stmt.where(ConsentRecord.purpose == purpose)
    if granted_only:
        stmt = stmt.where(
            ConsentRecord.is_granted.is_(True), ConsentRecord.withdrawn_at.is_(None)
        )
    if principal_ref:
        stmt = stmt.where(
            ConsentRecord.principal_fingerprint == fingerprint(principal_ref)
        )

    stmt = stmt.order_by(ConsentRecord.created_at.desc(), ConsentRecord.id.desc())
    rows, total = paginate(db, stmt, limit=limit, offset=offset)
    return Page[ConsentRecordResponse](
        items=[ConsentRecordResponse.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/consents",
    response_model=ConsentRecordResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a consent",
)
def create_consent(
    payload: ConsentCreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
):
    """Record one data principal's consent to one purpose.

    Consent under the Act is per-purpose, and the fiduciary must be able to
    prove *what* was consented to — so the notice version is mandatory and the
    notice text is stored alongside. "They agreed" is worthless without it.
    """
    consent = ConsentRecord(
        organization_id=ctx.org_id,
        principal_ref=encrypt(payload.principal_ref) or "",
        principal_fingerprint=fingerprint(payload.principal_ref) or "",
        principal_type=payload.principal_type,
        purpose=payload.purpose,
        purpose_description=payload.purpose_description,
        data_categories_json=payload.data_categories_json,
        notice_version=payload.notice_version,
        notice_text=payload.notice_text,
        notice_language=payload.notice_language,
        is_granted=True,
        granted_at=payload.granted_at or utcnow(),
        expires_at=payload.expires_at,
        collection_method=payload.collection_method,
        evidence_json=payload.evidence_json,
    )
    db.add(consent)
    db.flush()

    # The audit payload carries the *fingerprint*, never the identifier. The
    # trail is read by more people than the table it describes, so copying a
    # principal's email into it would widen exposure rather than record it.
    record(
        db,
        ctx,
        request,
        action=AuditAction.CREATE,
        entity_type="consent_record",
        entity_id=consent.id,
        after={
            "principal_fingerprint": consent.principal_fingerprint,
            "purpose": consent.purpose,
            "notice_version": consent.notice_version,
        },
        summary=f"Consent recorded for purpose {consent.purpose!r}",
    )
    db.commit()
    db.refresh(consent)

    response = ConsentRecordResponse.model_validate(consent)
    response.principal_ref = decrypt(consent.principal_ref)
    return response


@router.post(
    "/consents/{consent_id}/withdraw",
    response_model=ConsentRecordResponse,
    summary="Withdraw a consent",
)
def withdraw_consent(
    consent_id: int,
    payload: ConsentWithdrawRequest,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
):
    """Mark a consent withdrawn.

    The row stays. Withdrawal is a new state on it, not a deletion — the
    fiduciary still has to be able to show what was consented to, and for how
    long, before it was withdrawn.
    """
    consent = db.execute(
        scoped(ConsentRecord, ctx).where(ConsentRecord.id == consent_id)
    ).scalar_one_or_none()
    if consent is None:
        raise NotFoundError("No such consent record")
    if consent.withdrawn_at is not None:
        raise ConflictError("That consent has already been withdrawn")

    consent.is_granted = False
    consent.withdrawn_at = utcnow()
    evidence = dict(consent.evidence_json or {})
    if payload.reason:
        evidence["withdrawal_reason"] = payload.reason
    consent.evidence_json = evidence

    record(
        db,
        ctx,
        request,
        action=AuditAction.UPDATE,
        entity_type="consent_record",
        entity_id=consent.id,
        before={"is_granted": True},
        after={"is_granted": False, "reason": payload.reason},
        summary=f"Consent withdrawn for purpose {consent.purpose!r}",
    )
    db.commit()
    db.refresh(consent)
    return ConsentRecordResponse.model_validate(consent)


# --------------------------------------------------------------------------
# Personal data map
# --------------------------------------------------------------------------


@router.get(
    "/data-map", response_model=Page[DataMapEntryResponse], summary="The personal-data map"
)
def list_data_map(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sensitive_only: bool = Query(False),
    cross_border_only: bool = Query(False),
):
    stmt = scoped(DataMapEntry, ctx)
    if sensitive_only:
        stmt = stmt.where(DataMapEntry.is_sensitive.is_(True))
    if cross_border_only:
        stmt = stmt.where(DataMapEntry.is_transferred_abroad.is_(True))

    stmt = stmt.order_by(DataMapEntry.system_name, DataMapEntry.data_category)
    rows, total = paginate(db, stmt, limit=limit, offset=offset)
    return Page[DataMapEntryResponse](
        items=[DataMapEntryResponse.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/data-map",
    response_model=DataMapEntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a data-map entry",
)
def create_data_map_entry(
    payload: DataMapEntryRequest,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
):
    """One row of the personal-data inventory: what is held, where, and why."""
    entry = DataMapEntry(organization_id=ctx.org_id, **payload.model_dump())
    db.add(entry)
    db.flush()

    record(
        db,
        ctx,
        request,
        action=AuditAction.CREATE,
        entity_type="data_map_entry",
        entity_id=entry.id,
        after={
            "system_name": entry.system_name,
            "data_category": entry.data_category,
            "legal_basis": entry.legal_basis,
            "is_sensitive": entry.is_sensitive,
        },
        summary=f"Data map entry added for {entry.system_name}",
    )
    db.commit()
    db.refresh(entry)
    return DataMapEntryResponse.model_validate(entry)


@router.patch(
    "/data-map/{entry_id}",
    response_model=DataMapEntryResponse,
    summary="Update a data-map entry",
)
def update_data_map_entry(
    entry_id: int,
    payload: DataMapEntryRequest,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
):
    entry = db.execute(
        scoped(DataMapEntry, ctx).where(DataMapEntry.id == entry_id)
    ).scalar_one_or_none()
    if entry is None:
        raise NotFoundError("No such data map entry")

    for field, value in payload.model_dump().items():
        setattr(entry, field, value)
    # Editing an entry *is* reviewing it. The review date drives the "this
    # inventory has not been looked at in a year" gap on the readiness report,
    # and requiring a separate click to record it would leave it permanently
    # stale.
    entry.last_reviewed_at = utcnow()

    record(
        db,
        ctx,
        request,
        action=AuditAction.UPDATE,
        entity_type="data_map_entry",
        entity_id=entry.id,
        after={"system_name": entry.system_name, "data_category": entry.data_category},
        summary=f"Data map entry {entry.id} updated",
    )
    db.commit()
    db.refresh(entry)
    return DataMapEntryResponse.model_validate(entry)


@router.delete(
    "/data-map/{entry_id}", response_model=MessageResponse, summary="Remove a data-map entry"
)
def delete_data_map_entry(
    entry_id: int,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    entry = db.execute(
        scoped(DataMapEntry, ctx).where(DataMapEntry.id == entry_id)
    ).scalar_one_or_none()
    if entry is None:
        raise NotFoundError("No such data map entry")

    entry.soft_delete()
    record(
        db,
        ctx,
        request,
        action=AuditAction.SOFT_DELETE,
        entity_type="data_map_entry",
        entity_id=entry.id,
        before={"system_name": entry.system_name},
        summary=f"Data map entry for {entry.system_name} removed",
    )
    db.commit()
    return MessageResponse(message="Data map entry removed")


# --------------------------------------------------------------------------
# Breach register
# --------------------------------------------------------------------------


def _breach_response(breach: BreachIncident) -> BreachIncidentResponse:
    """Serialize a breach with its 72-hour clock computed."""
    response = BreachIncidentResponse.model_validate(breach)
    if breach.dpb_notified_at is None:
        deadline = breach.detected_at + timedelta(hours=BREACH_NOTIFICATION_HOURS)
        remaining = (deadline - utcnow()).total_seconds() / 3600
        response.hours_until_dpb_deadline = round(remaining, 2)
        response.dpb_notification_overdue = remaining < 0
    else:
        response.dpb_notification_overdue = False
    return response


@router.get(
    "/breaches", response_model=Page[BreachIncidentResponse], summary="The breach register"
)
def list_breaches(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    open_only: bool = Query(False),
    unnotified_only: bool = Query(False),
):
    stmt = scoped(BreachIncident, ctx)
    if open_only:
        stmt = stmt.where(BreachIncident.closed_at.is_(None))
    if unnotified_only:
        stmt = stmt.where(BreachIncident.dpb_notified_at.is_(None))

    stmt = stmt.order_by(BreachIncident.detected_at.desc(), BreachIncident.id.desc())
    rows, total = paginate(db, stmt, limit=limit, offset=offset)
    return Page[BreachIncidentResponse](
        items=[_breach_response(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/breaches",
    response_model=BreachIncidentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Report a breach",
)
def create_breach(
    payload: BreachCreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
):
    """Open a breach incident and start the 72-hour clock.

    ``detected_at`` defaults to now but is settable, because a breach is often
    entered hours after it was found and backdating it to the truth is what
    makes the deadline correct. Understating the response time in the register
    is the one thing that would make it worse than useless at an inquiry.
    """
    detected = payload.detected_at or utcnow()
    if payload.occurred_at is not None and payload.occurred_at > detected:
        raise ValidationError("A breach cannot be detected before it occurred")

    breach = BreachIncident(
        organization_id=ctx.org_id,
        reference=_next_reference(db, BreachIncident, ctx.org_id, "BR"),
        title=payload.title,
        description=payload.description,
        severity=payload.severity,
        status="open",
        occurred_at=payload.occurred_at,
        detected_at=detected,
        affected_principals_count=payload.affected_principals_count,
        affected_data_categories_json=payload.affected_data_categories_json,
        affected_systems_json=payload.affected_systems_json,
        reported_by_id=ctx.user_id,
    )
    db.add(breach)
    db.flush()

    record(
        db,
        ctx,
        request,
        action=AuditAction.CREATE,
        entity_type="breach_incident",
        entity_id=breach.id,
        after=snapshot(
            breach, ("reference", "severity", "status", "detected_at", "affected_principals_count")
        ),
        summary=(
            f"Breach {breach.reference} reported; the Data Protection Board must be "
            f"notified within {BREACH_NOTIFICATION_HOURS} hours of detection"
        ),
    )
    db.commit()
    db.refresh(breach)
    return _breach_response(breach)


@router.patch(
    "/breaches/{breach_id}",
    response_model=BreachIncidentResponse,
    summary="Update a breach",
)
def update_breach(
    breach_id: int,
    payload: BreachUpdateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
):
    breach = db.execute(
        scoped(BreachIncident, ctx).where(BreachIncident.id == breach_id)
    ).scalar_one_or_none()
    if breach is None:
        raise NotFoundError("No such breach incident")

    audited = ("severity", "status", "contained_at", "affected_principals_count")
    before = snapshot(breach, audited)

    changes = payload.model_dump(exclude_unset=True)
    if changes.get("status") == "closed" and breach.dpb_notified_at is None:
        raise ConflictError(
            "A breach cannot be closed before the Data Protection Board has been "
            "notified, or the notification explicitly recorded",
            details={"reference": breach.reference},
        )

    for field, value in changes.items():
        if value is not None:
            setattr(breach, field, value)

    if breach.status == "closed" and breach.closed_at is None:
        breach.closed_at = utcnow()
    if breach.status == "contained" and breach.contained_at is None:
        breach.contained_at = utcnow()

    record(
        db,
        ctx,
        request,
        action=AuditAction.UPDATE,
        entity_type="breach_incident",
        entity_id=breach.id,
        before=before,
        after=snapshot(breach, audited),
        summary=f"Breach {breach.reference} updated",
    )
    db.commit()
    db.refresh(breach)
    return _breach_response(breach)


@router.post(
    "/breaches/{breach_id}/notify",
    response_model=BreachIncidentResponse,
    summary="Record a breach notification",
)
def notify_breach(
    breach_id: int,
    payload: BreachNotifyRequest,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Record that the Board, or the affected principals, have been told.

    Compliance Manager and above: this is the statement that a statutory
    notification was made, and it is the single most consequential record in
    the DPDP module. The audit entry names how long after detection it
    happened, because that number is what an inquiry asks for.
    """
    breach = db.execute(
        scoped(BreachIncident, ctx).where(BreachIncident.id == breach_id)
    ).scalar_one_or_none()
    if breach is None:
        raise NotFoundError("No such breach incident")

    now = utcnow()
    if payload.notify_principals:
        breach.principals_notified_at = now
        if payload.notification_text:
            breach.principals_notification_text = payload.notification_text
        summary = f"Affected data principals notified for breach {breach.reference}"
    else:
        if breach.dpb_notified_at is not None:
            raise ConflictError(
                "The Data Protection Board has already been notified for this breach",
                details={"notified_at": breach.dpb_notified_at.isoformat()},
            )
        breach.dpb_notified_at = now
        breach.dpb_reference = payload.dpb_reference
        if payload.notification_text:
            breach.dpb_notification_text = payload.notification_text
        if breach.status == "open":
            breach.status = "notified"

        elapsed = (now - breach.detected_at).total_seconds() / 3600
        summary = (
            f"Data Protection Board notified for breach {breach.reference}, "
            f"{elapsed:.1f} hours after detection"
            + (
                f" — past the {BREACH_NOTIFICATION_HOURS}-hour window"
                if elapsed > BREACH_NOTIFICATION_HOURS
                else ""
            )
        )

    record(
        db,
        ctx,
        request,
        action=AuditAction.NOTIFY,
        entity_type="breach_incident",
        entity_id=breach.id,
        after={
            "dpb_notified_at": breach.dpb_notified_at.isoformat()
            if breach.dpb_notified_at
            else None,
            "dpb_reference": breach.dpb_reference,
            "principals_notified": payload.notify_principals,
        },
        summary=summary,
    )
    db.commit()
    db.refresh(breach)
    return _breach_response(breach)


# --------------------------------------------------------------------------
# Data subject requests
# --------------------------------------------------------------------------


def _dsr_response(dsr: DataSubjectRequest, today: date) -> DataSubjectRequestResponse:
    response = DataSubjectRequestResponse.model_validate(dsr)
    response.days_until_due = (dsr.due_date - today).days
    response.is_overdue = dsr.due_date < today and dsr.status not in (
        "completed",
        "rejected",
    )
    return response


@router.get(
    "/requests",
    response_model=Page[DataSubjectRequestResponse],
    summary="Data principal requests",
)
def list_data_requests(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    request_type: str | None = Query(None, max_length=32),
    open_only: bool = Query(False),
    overdue_only: bool = Query(False),
    today: date | None = Query(None),
):
    reference = today or date.today()
    stmt = scoped(DataSubjectRequest, ctx)
    if request_type:
        stmt = stmt.where(DataSubjectRequest.request_type == request_type)
    if open_only or overdue_only:
        stmt = stmt.where(DataSubjectRequest.status.notin_(["completed", "rejected"]))
    if overdue_only:
        stmt = stmt.where(DataSubjectRequest.due_date < reference)

    stmt = stmt.order_by(DataSubjectRequest.due_date, DataSubjectRequest.id)
    rows, total = paginate(db, stmt, limit=limit, offset=offset)
    return Page[DataSubjectRequestResponse](
        items=[_dsr_response(r, reference) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/requests",
    response_model=DataSubjectRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Log a data principal request",
)
def create_data_request(
    payload: DataSubjectRequestCreate,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
):
    """Log a request and start the SLA clock.

    The due date is computed from ``received_at``, not from now: a request
    logged three days after it arrived is already three days into its window,
    and dating the clock from data entry would systematically overstate how
    much time is left.
    """
    received = payload.received_at or utcnow()
    dsr = DataSubjectRequest(
        organization_id=ctx.org_id,
        reference=_next_reference(db, DataSubjectRequest, ctx.org_id, "DSR"),
        request_type=payload.request_type,
        principal_ref=encrypt(payload.principal_ref) or "",
        principal_fingerprint=fingerprint(payload.principal_ref) or "",
        principal_name=payload.principal_name,
        details=payload.details,
        status="received",
        received_at=received,
        due_date=(received + timedelta(days=DSR_SLA_DAYS)).date(),
        assigned_to_id=payload.assigned_to_id,
    )
    db.add(dsr)
    db.flush()

    record(
        db,
        ctx,
        request,
        action=AuditAction.CREATE,
        entity_type="data_subject_request",
        entity_id=dsr.id,
        after={
            "reference": dsr.reference,
            "request_type": dsr.request_type,
            "principal_fingerprint": dsr.principal_fingerprint,
            "due_date": dsr.due_date.isoformat(),
        },
        summary=(
            f"{dsr.request_type} request {dsr.reference} logged; "
            f"due {dsr.due_date.isoformat()}"
        ),
    )
    db.commit()
    db.refresh(dsr)

    response = _dsr_response(dsr, date.today())
    response.principal_ref = decrypt(dsr.principal_ref)
    return response


@router.patch(
    "/requests/{request_id}",
    response_model=DataSubjectRequestResponse,
    summary="Progress a data principal request",
)
def update_data_request(
    request_id: int,
    payload: DataSubjectRequestUpdate,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
):
    dsr = db.execute(
        scoped(DataSubjectRequest, ctx).where(DataSubjectRequest.id == request_id)
    ).scalar_one_or_none()
    if dsr is None:
        raise NotFoundError("No such data subject request")

    audited = ("status", "assigned_to_id", "verified_at", "completed_at")
    before = snapshot(dsr, audited)

    changes = payload.model_dump(exclude_unset=True)
    if changes.pop("mark_verified", False) and dsr.verified_at is None:
        dsr.verified_at = utcnow()

    for field, value in changes.items():
        if value is not None:
            setattr(dsr, field, value)

    if dsr.status in ("completed", "rejected") and dsr.completed_at is None:
        dsr.completed_at = utcnow()

    record(
        db,
        ctx,
        request,
        action=AuditAction.UPDATE,
        entity_type="data_subject_request",
        entity_id=dsr.id,
        before=before,
        after=snapshot(dsr, audited),
        summary=f"Request {dsr.reference} updated to {dsr.status}",
    )
    db.commit()
    db.refresh(dsr)
    return _dsr_response(dsr, date.today())


# --------------------------------------------------------------------------
# Privacy impact assessments
# --------------------------------------------------------------------------


@router.get(
    "/assessments", response_model=Page[PIAResponse], summary="Privacy impact assessments"
)
def list_assessments(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(None, alias="status", max_length=32),
):
    stmt = scoped(PrivacyImpactAssessment, ctx)
    if status_filter:
        stmt = stmt.where(PrivacyImpactAssessment.status == status_filter)

    stmt = stmt.order_by(PrivacyImpactAssessment.created_at.desc())
    rows, total = paginate(db, stmt, limit=limit, offset=offset)
    return Page[PIAResponse](
        items=[PIAResponse.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/assessments",
    response_model=PIAResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a privacy impact assessment",
)
def create_assessment(
    payload: PIARequest,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
):
    pia = PrivacyImpactAssessment(
        organization_id=ctx.org_id, status="draft", **payload.model_dump()
    )
    db.add(pia)
    db.flush()

    record(
        db,
        ctx,
        request,
        action=AuditAction.CREATE,
        entity_type="privacy_impact_assessment",
        entity_id=pia.id,
        after={"title": pia.title, "status": pia.status},
        summary=f"Privacy impact assessment started: {pia.title}",
    )
    db.commit()
    db.refresh(pia)
    return PIAResponse.model_validate(pia)


@router.post(
    "/assessments/{assessment_id}/review",
    response_model=PIAResponse,
    summary="Sign off an assessment",
)
def review_assessment(
    assessment_id: int,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
    next_review_months: int = Query(12, ge=1, le=60),
):
    """Record a human sign-off on an assessment.

    An assessment whose risk score came from a model is a draft until somebody
    accountable has read it. This is that record, and it is Compliance Manager
    and above for the same reason approving a filing is.
    """
    pia = db.execute(
        scoped(PrivacyImpactAssessment, ctx).where(
            PrivacyImpactAssessment.id == assessment_id
        )
    ).scalar_one_or_none()
    if pia is None:
        raise NotFoundError("No such assessment")

    pia.status = "approved"
    pia.reviewed_by_id = ctx.user_id
    pia.reviewed_at = utcnow()
    today = date.today()
    # Rolled forward by whole months with the day clamped, so a review on the
    # 31st does not land on a date that does not exist.
    month = today.month - 1 + next_review_months
    year = today.year + month // 12
    month = month % 12 + 1
    import calendar as _calendar

    pia.next_review_date = date(
        year, month, min(today.day, _calendar.monthrange(year, month)[1])
    )

    record(
        db,
        ctx,
        request,
        action=AuditAction.APPROVE,
        entity_type="privacy_impact_assessment",
        entity_id=pia.id,
        after={
            "status": pia.status,
            "reviewed_by_id": ctx.user_id,
            "next_review_date": pia.next_review_date.isoformat(),
        },
        summary=f"Assessment {pia.title!r} approved",
    )
    db.commit()
    db.refresh(pia)
    return PIAResponse.model_validate(pia)


# --------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------


@router.get(
    "/readiness", response_model=DPDPReadinessResponse, summary="DPDP readiness"
)
def readiness(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    today: date | None = Query(None),
):
    """A coarse readiness score, and the gaps behind it.

    Deliberately coarse. The Act's Rules are not fully notified, and a score
    with two decimal places would imply an accuracy about compliance with an
    unfinished regulation that nobody has. What the gaps list says is worth
    more than the number.
    """
    reference = today or date.today()
    now = utcnow()

    def count(stmt) -> int:
        return int(
            db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
        )

    data_map_entries = count(scoped(DataMapEntry, ctx))
    consents = count(scoped(ConsentRecord, ctx))
    active_consents = count(
        scoped(ConsentRecord, ctx).where(
            ConsentRecord.is_granted.is_(True), ConsentRecord.withdrawn_at.is_(None)
        )
    )
    withdrawn = count(
        scoped(ConsentRecord, ctx).where(ConsentRecord.withdrawn_at.is_not(None))
    )
    open_breaches = count(
        scoped(BreachIncident, ctx).where(BreachIncident.closed_at.is_(None))
    )
    overdue_notifications = count(
        scoped(BreachIncident, ctx).where(
            BreachIncident.dpb_notified_at.is_(None),
            BreachIncident.detected_at
            < now - timedelta(hours=BREACH_NOTIFICATION_HOURS),
        )
    )
    open_requests_stmt = scoped(DataSubjectRequest, ctx).where(
        DataSubjectRequest.status.notin_(["completed", "rejected"])
    )
    open_requests = count(open_requests_stmt)
    overdue_requests = count(
        open_requests_stmt.where(DataSubjectRequest.due_date < reference)
    )
    completed_assessments = count(
        scoped(PrivacyImpactAssessment, ctx).where(
            PrivacyImpactAssessment.status == "approved"
        )
    )
    cross_border = count(
        scoped(DataMapEntry, ctx).where(DataMapEntry.is_transferred_abroad.is_(True))
    )

    # Weighted so that the two things with a statutory clock — a missed Board
    # notification and an overdue principal request — cost more than an
    # incomplete inventory, which is bad practice but not yet a breach of a
    # deadline.
    gaps: list[str] = []
    score = 100
    if data_map_entries == 0:
        score -= 30
        gaps.append("No personal-data map: the inventory the Act's obligations rest on")
    if consents == 0:
        score -= 15
        gaps.append("No consent records: consent under the Act must be provable")
    if overdue_notifications:
        score -= 25
        gaps.append(
            f"{overdue_notifications} breach(es) past the "
            f"{BREACH_NOTIFICATION_HOURS}-hour Data Protection Board window"
        )
    if overdue_requests:
        score -= 15
        gaps.append(f"{overdue_requests} data principal request(s) past the SLA")
    if completed_assessments == 0 and data_map_entries:
        score -= 10
        gaps.append("No approved privacy impact assessment")
    if cross_border and completed_assessments == 0:
        score -= 5
        gaps.append("Cross-border transfers recorded with no assessment behind them")

    return DPDPReadinessResponse(
        has_data_map=data_map_entries > 0,
        data_map_entries=data_map_entries,
        consent_records=consents,
        active_consents=active_consents,
        withdrawn_consents=withdrawn,
        open_breaches=open_breaches,
        overdue_dpb_notifications=overdue_notifications,
        open_data_requests=open_requests,
        overdue_data_requests=overdue_requests,
        completed_assessments=completed_assessments,
        cross_border_transfers=cross_border,
        score=max(0, min(100, score)),
        gaps=gaps,
    )
