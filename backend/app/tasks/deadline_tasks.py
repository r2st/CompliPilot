"""Scheduled work on deadlines: reminders, generation, escalation (section 4.1).

Three tasks, all idempotent, because ``task_acks_late`` means any of them may
run twice when a worker dies mid-body:

* :func:`sweep_deadline_reminders` — hourly. Sends the reminder offsets that
  are due and not yet sent.
* :func:`generate_upcoming_filings` — nightly. Fills the calendar forward.
* :func:`escalate_overdue` — daily. Raises the alarm on filings past their date.

Idempotence comes from state on the ``Deadline`` row rather than from the
scheduler: ``reminders_sent_json`` records which offsets have gone out and
``escalation_level`` records how far an overdue filing has been pushed. A task
that runs twice in an hour finds its work already recorded and does nothing.
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.models.enums import AuditAction, NotificationChannel, UserRole
from app.models.filing import Deadline, Filing
from app.models.mixins import utcnow
from app.models.organization import Organization
from app.services import audit as audit_service
from app.services import notifications
from app.services.filing_generator import generate_for_organization
from app.tasks.base import SYSTEM_ACTOR, active_organizations, per_organization, task_session

logger = logging.getLogger(__name__)

# Which roles hear about a deadline at each escalation level. Level 0 is the
# ordinary reminder; each step up widens the audience rather than replacing it,
# because the point of an escalation is that more senior people learn the
# filing is still open — not that the person doing the work stops being told.
ESCALATION_AUDIENCE: dict[int, set[UserRole]] = {
    0: {UserRole.STAFF, UserRole.COMPLIANCE_MANAGER},
    1: {UserRole.STAFF, UserRole.COMPLIANCE_MANAGER},
    2: {UserRole.STAFF, UserRole.COMPLIANCE_MANAGER, UserRole.ADMIN},
    3: {UserRole.STAFF, UserRole.COMPLIANCE_MANAGER, UserRole.ADMIN},
}

# Days overdue at which each escalation level is reached.
ESCALATION_THRESHOLDS: tuple[tuple[int, int], ...] = ((1, 1), (7, 2), (30, 3))

MAX_ESCALATION_LEVEL = 3


def _escalation_level_for(days_overdue: int) -> int:
    """The level a filing *days_overdue* days late should be at."""
    level = 0
    for threshold, value in ESCALATION_THRESHOLDS:
        if days_overdue >= threshold:
            level = value
    return level


def _audience_for(level: int) -> set[UserRole]:
    return ESCALATION_AUDIENCE.get(min(level, MAX_ESCALATION_LEVEL), ESCALATION_AUDIENCE[0])


def _open_deadlines(db: Session, organization_id: int) -> list[Deadline]:
    """Unsatisfied deadlines for one organization, with their filings.

    Satisfied ones are excluded in SQL rather than in Python: the index leads
    on ``is_satisfied`` precisely so a sweep never scans the growing history of
    closed deadlines.
    """
    return list(
        db.execute(
            select(Deadline)
            .join(Filing, Filing.id == Deadline.filing_id)
            .where(
                Deadline.organization_id == organization_id,
                Deadline.is_satisfied.is_(False),
                Deadline.deleted_at.is_(None),
                Filing.deleted_at.is_(None),
            )
            .order_by(Deadline.due_date)
        )
        .scalars()
        .all()
    )


def _notify(
    db: Session,
    org: Organization,
    filing: Filing,
    *,
    subject: str,
    body: str,
    kind: str,
    roles: set[UserRole],
    reminder_offset_days: int | None = None,
) -> int:
    """Send one message to everyone who should hear it. Returns the count sent."""
    recipients = notifications.recipients_for(
        db,
        org.id,
        roles=roles,
        channels={NotificationChannel.EMAIL, NotificationChannel.IN_APP},
        # A one-person practice has an Admin and nobody else. Without this the
        # ladder's level-0 audience resolves to nobody and they would silently
        # receive no reminders at all.
        fallback_roles={UserRole.ADMIN},
    )
    for recipient in recipients:
        notifications.send(
            db,
            organization_id=org.id,
            recipient=recipient,
            subject=subject,
            content=body,
            kind=kind,
            entity_type="filing",
            entity_id=filing.id,
            reminder_offset_days=reminder_offset_days,
        )
    return len(recipients)


def _sweep_org_reminders(db: Session, org: Organization, *, today: date) -> dict:
    """Send every reminder due for one organization.

    Quiet hours are checked once per organization rather than per deadline: the
    answer cannot differ between two filings in the same sweep, and asking per
    filing would be a preference lookup per row.
    """
    pref = notifications.preferences_for(db, org.id)
    if notifications.in_quiet_hours(pref):
        return {"organization_id": org.id, "skipped_quiet_hours": True, "sent": 0}

    offsets = notifications.reminder_offsets_for(db, org.id)
    sent = 0

    for deadline in _open_deadlines(db, org.id):
        filing = deadline.filing
        days_until_due = (deadline.due_date - today).days

        # The *tightest* band the deadline currently falls in — the smallest
        # configured offset still at or above the days remaining. Taking the
        # largest instead looks equivalent and is not: a filing seven days out
        # would match the 30-day offset, send it, then match 15 on the next
        # hourly sweep and 7 on the one after, delivering five reminders in
        # five hours for a single deadline.
        band = min((o for o in offsets if days_until_due <= o), default=None)
        if band is None or deadline.has_sent(band):
            continue

        subject, body = notifications.deadline_reminder_message(
            filing, days_until_due=days_until_due, organization_name=org.name
        )
        _notify(
            db,
            org,
            filing,
            subject=subject,
            body=body,
            kind="deadline_reminder",
            roles=_audience_for(0),
            reminder_offset_days=band,
        )

        # Every wider offset is recorded alongside the one just sent. Their
        # moment has passed — a 30-day reminder for a filing due in three days
        # is not a thing to send later — and leaving them unrecorded would let
        # a reopened filing replay the whole schedule from the top.
        for offset in offsets:
            if offset >= band:
                deadline.record_sent(offset)
        deadline.last_reminder_at = utcnow()
        sent += 1

    db.flush()
    return {"organization_id": org.id, "sent": sent}


@celery_app.task(name="app.tasks.deadline_tasks.sweep_deadline_reminders")
def sweep_deadline_reminders(organization_ids: list[int] | None = None) -> dict:
    """Hourly: send the deadline reminders that have come due."""
    today = notifications.today_ist()
    with task_session() as db:
        orgs = active_organizations(db, organization_ids=organization_ids)
        results = per_organization(
            db,
            orgs,
            lambda session, org: _sweep_org_reminders(session, org, today=today),
            task_name="sweep_deadline_reminders",
        )
    total = sum(r.get("sent", 0) for r in results)
    logger.info("Deadline reminder sweep: %s organizations, %s reminders", len(results), total)
    return {"organizations": len(results), "reminders_sent": total}


@celery_app.task(name="app.tasks.deadline_tasks.generate_upcoming_filings")
def generate_upcoming_filings(
    organization_ids: list[int] | None = None, horizon_days: int | None = None
) -> dict:
    """Nightly: create the filings each organization's obligations imply.

    Delegates to :func:`app.services.filing_generator.generate_for_organization`,
    which is idempotent — this task exists to schedule it and to record that it
    ran, not to duplicate its logic.
    """
    today = notifications.today_ist()

    def _generate(db: Session, org: Organization) -> dict:
        kwargs = {"today": today}
        if horizon_days is not None:
            kwargs["horizon_days"] = horizon_days
        result = generate_for_organization(db, org, **kwargs)
        if result.created:
            audit_service.record(
                db,
                organization_id=org.id,
                action=AuditAction.GENERATE,
                entity_type="filing",
                actor_label=SYSTEM_ACTOR,
                summary=f"Scheduled generation created {result.created} filing(s)",
                after={"created": result.created, "filing_ids": result.filing_ids[:50]},
            )
        return result.as_dict()

    with task_session() as db:
        orgs = active_organizations(db, organization_ids=organization_ids)
        results = per_organization(
            db, orgs, _generate, task_name="generate_upcoming_filings"
        )
    created = sum(r.get("created", 0) for r in results)
    logger.info("Filing generation sweep: %s organizations, %s created", len(results), created)
    return {"organizations": len(results), "created": created}


def _escalate_org(db: Session, org: Organization, *, today: date) -> dict:
    """Raise the escalation level on this organization's overdue filings.

    A notification goes out only when the level actually *changes*. Without
    that check a filing thirty days overdue would send an identical escalation
    every morning, and the people who most need to act on it would have
    filtered the sender by the end of the week.
    """
    escalated = 0

    for deadline in _open_deadlines(db, org.id):
        days_overdue = (today - deadline.due_date).days
        if days_overdue < 1:
            continue

        target = _escalation_level_for(days_overdue)
        if target <= deadline.escalation_level:
            continue

        filing = deadline.filing
        deadline.escalation_level = target

        subject, body = notifications.escalation_message(
            filing, days_overdue=days_overdue, level=target, organization_name=org.name
        )
        _notify(
            db,
            org,
            filing,
            subject=subject,
            body=body,
            kind="escalation",
            roles=_audience_for(target),
        )
        audit_service.record(
            db,
            organization_id=org.id,
            action=AuditAction.NOTIFY,
            entity_type="filing",
            entity_id=filing.id,
            actor_label=SYSTEM_ACTOR,
            summary=(
                f"Escalated to level {target}: {days_overdue} day(s) overdue"
            ),
            after={"escalation_level": target, "days_overdue": days_overdue},
        )
        escalated += 1

    db.flush()
    return {"organization_id": org.id, "escalated": escalated}


@celery_app.task(name="app.tasks.deadline_tasks.escalate_overdue")
def escalate_overdue(organization_ids: list[int] | None = None) -> dict:
    """Daily: escalate filings that are past their due date and still open."""
    today = notifications.today_ist()
    with task_session() as db:
        orgs = active_organizations(db, organization_ids=organization_ids)
        results = per_organization(
            db,
            orgs,
            lambda session, org: _escalate_org(session, org, today=today),
            task_name="escalate_overdue",
        )
    total = sum(r.get("escalated", 0) for r in results)
    logger.info("Escalation sweep: %s organizations, %s escalated", len(results), total)
    return {"organizations": len(results), "escalated": total}
