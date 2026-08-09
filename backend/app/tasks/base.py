"""Shared plumbing for the scheduled tasks.

The tasks run outside a request, so nothing supplies them a session, a tenant
context or an actor. This module is where those are manufactured, in one place,
so that a task body is about the work rather than about its own scaffolding.

**Why a session per task rather than per sweep item.** A sweep that opens one
session and holds it for four hundred organizations holds one connection and
one transaction for the whole run; a single failure late in the sweep would
roll back every organization processed before it. :func:`task_session` gives
the sweep its session, and the sweep commits per organization — so one tenant's
bad data costs that tenant's iteration, not the run.
"""
from __future__ import annotations

import logging
from collections.abc import Generator, Iterable
from contextlib import contextmanager

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.organization import Organization

logger = logging.getLogger(__name__)

# The actor recorded on audit entries a scheduler wrote. Distinct from any real
# user so that "who marked this filing late" has an honest answer rather than
# naming whichever admin happened to be first in the table.
SYSTEM_ACTOR = "system:scheduler"


@contextmanager
def task_session() -> Generator[Session, None, None]:
    """A database session scoped to one task run.

    Rolls back and closes on the way out. The rollback is not redundant with
    the close: a task that raised partway through has uncommitted work on the
    connection, and returning it to the pool dirty gives the next borrower a
    transaction it did not open.
    """
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def active_organizations(
    db: Session, *, organization_ids: Iterable[int] | None = None
) -> list[Organization]:
    """Every organization a sweep should visit.

    Soft-deleted and deactivated tenants are excluded here rather than in each
    caller, because a sweep that reminded a cancelled customer about their GST
    deadline is a support ticket and each task would otherwise have to remember
    the same two filters.
    """
    stmt = select(Organization).where(
        Organization.deleted_at.is_(None),
        Organization.is_active.is_(True),
    )
    ids = list(organization_ids) if organization_ids else None
    if ids:
        stmt = stmt.where(Organization.id.in_(ids))
    return list(db.execute(stmt.order_by(Organization.id)).scalars().all())


def per_organization(
    db: Session,
    organizations: Iterable[Organization],
    handler,
    *,
    task_name: str,
) -> list[dict]:
    """Run *handler* for each organization, committing and isolating each.

    The isolation is the point. These run unattended at 01:00; a tenant with a
    malformed profile must not stop the other 399 from getting their calendar,
    and a sweep that rolled everything back on the last failure would be a
    sweep that never succeeds.
    """
    results: list[dict] = []
    for org in organizations:
        try:
            outcome = handler(db, org)
            db.commit()
            if outcome is not None:
                results.append(outcome)
        except Exception:  # noqa: BLE001 - one tenant must not stop the sweep
            db.rollback()
            logger.exception("%s failed for organization %s", task_name, org.id)
            results.append({"organization_id": org.id, "error": True})
    return results
