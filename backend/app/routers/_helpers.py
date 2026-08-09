"""Small pieces every router repeats, written once.

Nothing here is clever. It is here because the alternative is eleven routers
each spelling "fetch this row, scoped to the tenant, or 404" slightly
differently — and the one that spells it differently is the one that forgets
the tenant filter.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, TypeVar

from fastapi import Request
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.deps import client_ip
from app.core.errors import ForbiddenError, NotFoundError
from app.core.tenancy import TenantContext, scoped
from app.models.enums import AuditAction
from app.services import audit as audit_service

T = TypeVar("T")


def audit_meta(request: Request) -> dict[str, str | None]:
    """The request fields every audit entry carries.

    Pulled from one place so an entry written by a route that forgot the user
    agent is impossible rather than merely unlikely.
    """
    return {
        "ip_address": client_ip(request),
        "user_agent": request.headers.get("user-agent"),
        "request_id": getattr(request.state, "request_id", None),
    }


def record(
    db: Session,
    ctx: TenantContext,
    request: Request,
    *,
    action: AuditAction | str,
    entity_type: str,
    entity_id: Any = None,
    before: dict | None = None,
    after: dict | None = None,
    summary: str | None = None,
) -> None:
    """Append to the audit chain with the request metadata already filled in."""
    audit_service.record_for(
        db,
        ctx,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
        summary=summary,
        **audit_meta(request),  # type: ignore[arg-type]
    )


def get_scoped(
    db: Session,
    model: type[T],
    entity_id: int,
    ctx: TenantContext,
    *,
    label: str | None = None,
    include_deleted: bool = False,
) -> T:
    """One row of *model*, in this tenant, or a 404.

    A row belonging to another tenant answers 404, not 403. 403 would confirm
    the id exists, which lets one CA firm walk the id space and learn how many
    filings a competitor's client has.
    """
    name = label or model.__name__  # type: ignore[attr-defined]
    row = db.execute(
        scoped(model, ctx, include_deleted=include_deleted).where(
            model.id == entity_id  # type: ignore[attr-defined]
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No such {name.lower()}")
    return row


def paginate(db: Session, stmt: Select, *, limit: int, offset: int) -> tuple[list, int]:
    """``(rows, total)`` for one page.

    The count is a second query against the same filters with the ordering
    stripped — ORDER BY on a COUNT is wasted work that Postgres will not always
    optimise away, and on a filings table with a million rows it is measurable.
    """
    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()
    rows = list(db.execute(stmt.limit(limit).offset(offset)).scalars().all())
    return rows, int(total)


def changed_fields(before: dict, after: dict) -> dict[str, dict]:
    """A compact before/after diff for the audit payload.

    Only the keys that moved. An audit entry that repeats all forty columns of
    an organization row every time someone fixes a typo in the address is one
    nobody will read, and the trail is only useful if it is read.
    """
    return {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in set(before) | set(after)
        if before.get(key) != after.get(key)
    }


def snapshot(row: Any, fields: tuple[str, ...]) -> dict:
    """A plain dict of *fields* off *row*, for the audit payload.

    Enums and dates are stringified here rather than left to the JSON encoder.
    The audit payload is both stored in a JSON column *and* hashed into the
    chain checksum, and those two must see the same bytes — a value that the
    column serialises one way and :func:`app.services.audit._canonical`
    another would verify as tampered on a row nobody touched.
    """
    out: dict[str, Any] = {}
    for field in fields:
        value = getattr(row, field, None)
        if isinstance(value, Enum):
            out[field] = str(value)
        elif isinstance(value, datetime | date):
            out[field] = value.isoformat()
        else:
            out[field] = value
    return out


def deny_system_row(row: Any, *, label: str) -> None:
    """Refuse a write to a CompliPilot-maintained catalogue or template row.

    System rows have ``organization_id IS NULL`` and are visible to every
    tenant through :func:`app.core.tenancy.catalogue_scoped`. Visible is not
    writable: one tenant editing the shared GSTR-3B definition would change it
    for everyone.
    """
    if getattr(row, "is_system", False) or getattr(row, "organization_id", 0) is None:
        raise ForbiddenError(
            f"This {label} is maintained by CompliPilot and cannot be modified. "
            "Create your own copy instead."
        )
