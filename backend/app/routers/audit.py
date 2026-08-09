"""Reading and verifying the append-only audit trail (section 4.7).

There is no write endpoint here, and there never will be. The only writer is
:func:`app.services.audit.record`, called from inside the transaction of the
thing being recorded — so an action that rolls back leaves no entry claiming it
happened, and an entry cannot be created without an action.

There is no delete or update endpoint either. The table is append-only by
database trigger as well as by convention; see the
``audit_trail_append_only_triggers`` migration.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_admin, require_manager
from app.core.tenancy import TenantContext
from app.models.audit import AuditTrail
from app.models.enums import AuditAction
from app.routers._helpers import audit_meta, paginate
from app.schemas.audit import (
    AuditEntryResponse,
    ChainHeadResponse,
    ChainVerificationResponse,
)
from app.schemas.common import Page
from app.services import audit as audit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audit", tags=["audit"])

# An export longer than this is a data extract, not an audit review, and
# streaming a million rows through the API is not how it should be taken.
_MAX_EXPORT_ROWS = 50_000


def _scoped(ctx: TenantContext):
    """The chain for the organization in context.

    Not :func:`app.core.tenancy.scoped` — ``AuditTrail`` carries no
    ``deleted_at`` because nothing in it is ever deleted, and passing it
    through a helper that filters on soft deletes would only obscure that.
    """
    return select(AuditTrail).where(AuditTrail.organization_id == ctx.org_id)


@router.get("", response_model=Page[AuditEntryResponse], summary="Read the audit trail")
def list_entries(
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    entity_type: str | None = Query(None, max_length=64),
    entity_id: str | None = Query(None, max_length=64),
    action: AuditAction | None = None,
    user_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
):
    """The trail, newest first.

    Compliance Manager and above. The trail names who did what from which IP,
    which is more than a Staff account needs and exactly what a departing
    employee would want to read.
    """
    stmt = _scoped(ctx)
    if entity_type:
        stmt = stmt.where(AuditTrail.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditTrail.entity_id == entity_id)
    if action is not None:
        stmt = stmt.where(AuditTrail.action == action)
    if user_id is not None:
        stmt = stmt.where(AuditTrail.user_id == user_id)
    if start_date is not None:
        stmt = stmt.where(AuditTrail.timestamp >= datetime.combine(start_date, time.min))
    if end_date is not None:
        # Inclusive of the end date, which is what a person means by "to the
        # 14th". Comparing against the bare date would silently exclude
        # everything that happened on it.
        stmt = stmt.where(AuditTrail.timestamp <= datetime.combine(end_date, time.max))

    stmt = stmt.order_by(AuditTrail.sequence.desc())
    rows, total = paginate(db, stmt, limit=limit, offset=offset)
    return Page[AuditEntryResponse](
        items=[AuditEntryResponse.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/entity/{entity_type}/{entity_id}",
    response_model=list[AuditEntryResponse],
    summary="One record's history",
)
def entity_history(
    entity_type: str,
    entity_id: str,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
    limit: int = Query(200, ge=1, le=500),
):
    """Everything that has happened to one record, oldest first.

    Oldest first here, unlike the main list: this is read as a narrative — who
    drafted it, who reviewed it, when it was filed — and a story told backwards
    is harder to follow than one told forwards.
    """
    rows = (
        db.execute(
            _scoped(ctx)
            .where(
                AuditTrail.entity_type == entity_type,
                AuditTrail.entity_id == str(entity_id),
            )
            .order_by(AuditTrail.sequence)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [AuditEntryResponse.model_validate(r) for r in rows]


@router.get(
    "/verify",
    response_model=ChainVerificationResponse,
    summary="Verify the chain has not been altered",
)
def verify(
    ctx: TenantContext = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Recompute every checksum in this organization's chain.

    Admin only. A tampered chain is an incident, and the answer to "is our
    audit trail intact" should not be casually browsable.

    Note what this does and does not prove. It proves history has not been
    edited in place. It cannot detect a truncation — an attacker with write
    access holds the same key material the writer does and can re-chain from
    any point. Detecting that needs the head checksum published somewhere the
    database cannot reach, which is what ``/audit/head`` is for.
    """
    result = audit_service.verify_chain(db, ctx.org_id)
    if not result.is_valid:
        logger.error(
            "Audit chain verification FAILED for organization %s at sequence %s: %s",
            ctx.org_id,
            result.broken_at_sequence,
            result.reason,
        )
    return ChainVerificationResponse(**result.as_dict())


@router.get(
    "/head", response_model=ChainHeadResponse, summary="The chain's current head"
)
def head(
    ctx: TenantContext = Depends(require_manager), db: Session = Depends(get_db)
):
    """The chain's length and last checksum, for publishing outside the database.

    Emailing this to the compliance officer weekly, or committing it, turns
    "the chain is internally consistent" into "the chain matches what we
    published on the 14th" — which is the check that catches a truncation.
    """
    return ChainHeadResponse(**audit_service.chain_head(db, ctx.org_id))


@router.get("/export", summary="Export the trail as CSV")
def export_csv(
    request: Request,
    ctx: TenantContext = Depends(require_admin),
    db: Session = Depends(get_db),
    start_date: date | None = None,
    end_date: date | None = None,
    entity_type: str | None = Query(None, max_length=64),
):
    """Stream the trail as CSV, for handing to an auditor.

    The export is itself recorded in the trail — the last row of the file will
    not include its own entry, and that is correct: the entry is written before
    the stream is built, so the *next* export shows that this one happened.

    Streamed rather than assembled, so a four-year trail does not become a
    four-year trail held in memory. The checksum columns are included, so the
    recipient can verify the chain independently of us.
    """
    stmt = _scoped(ctx)
    if start_date is not None:
        stmt = stmt.where(AuditTrail.timestamp >= datetime.combine(start_date, time.min))
    if end_date is not None:
        stmt = stmt.where(AuditTrail.timestamp <= datetime.combine(end_date, time.max))
    if entity_type:
        stmt = stmt.where(AuditTrail.entity_type == entity_type)

    own_entry, *_ = audit_service.record_for(
        db,
        ctx,
        action=AuditAction.EXPORT,
        entity_type="audit_trail",
        summary=(
            "Audit trail exported"
            + (f" from {start_date.isoformat()}" if start_date else "")
            + (f" to {end_date.isoformat()}" if end_date else "")
        ),
        **audit_meta(request),  # type: ignore[arg-type]
    )
    db.commit()

    # Bounded below this export's own entry. The rows are not read until the
    # response streams, which is after the entry above has been committed — so
    # without this the file ends with the record of its own creation, carrying
    # a checksum the recipient has no way to have seen. Written first and
    # excluded here, the export shows up in the *next* export instead, which is
    # where a reader counting exports would look for it.
    stmt = stmt.where(AuditTrail.sequence < own_entry.sequence)
    stmt = stmt.order_by(AuditTrail.sequence).limit(_MAX_EXPORT_ROWS)

    columns = (
        "sequence",
        "timestamp",
        "actor_label",
        "user_id",
        "action",
        "entity_type",
        "entity_id",
        "summary",
        "ip_address",
        "request_id",
        "prev_checksum",
        "checksum",
    )

    def rows():
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        writer.writerow(columns)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

        # ``yield_per`` so the driver streams rather than buffering the whole
        # result set — the point of the generator is defeated if SQLAlchemy has
        # already materialised every row before the first one is written.
        for entry in db.execute(stmt).scalars().yield_per(500):
            writer.writerow(
                [
                    entry.sequence,
                    entry.timestamp.isoformat(),
                    entry.actor_label,
                    entry.user_id or "",
                    str(entry.action),
                    entry.entity_type,
                    entry.entity_id or "",
                    entry.summary or "",
                    entry.ip_address or "",
                    entry.request_id or "",
                    entry.prev_checksum,
                    entry.checksum,
                ]
            )
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    filename = f"audit-trail-org{ctx.org_id}-{date.today().isoformat()}.csv"
    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
