"""Scheduled notification work: retrying failures, and the DPDP breach clock.

:func:`retry_failed` is the safety net under every other sender — a reminder
that could not go out because the mail host was briefly unreachable is retried
here rather than lost. :func:`check_breach_deadlines` watches the DPDP Act's
72-hour notification window, which is short enough that a daily check would be
too coarse to be useful.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.models.dpdp import BreachIncident
from app.models.enums import AuditAction, NotificationChannel, NotificationStatus, UserRole
from app.models.mixins import utcnow
from app.models.notification import Notification
from app.models.organization import Organization
from app.services import audit as audit_service
from app.services import notifications
from app.tasks.base import SYSTEM_ACTOR, active_organizations, per_organization, task_session

logger = logging.getLogger(__name__)

# How many notifications one retry pass will attempt. A bound, so that a mail
# host that was down overnight produces a steady catch-up rather than one task
# that tries nine thousand sends inside a ten-minute time limit and is killed
# having committed none of them.
RETRY_BATCH = 200

# Hours before the 72-hour deadline at which to warn. Two warnings: one with
# most of the window left, one when it is nearly gone.
BREACH_WARNING_HOURS: tuple[float, ...] = (24.0, 6.0)


@celery_app.task(name="app.tasks.notification_tasks.retry_failed")
def retry_failed(limit: int = RETRY_BATCH) -> dict:
    """Every 20 minutes: re-attempt notifications that failed.

    Only ``FAILED`` rows under the attempt ceiling are picked up. ``SKIPPED``
    is deliberately excluded — it means the channel has no credentials, so a
    retry cannot succeed and would burn the attempt budget of a row that might
    otherwise be retried when the configuration is fixed.
    """
    retried = 0
    succeeded = 0

    with task_session() as db:
        rows = (
            db.execute(
                select(Notification)
                .where(
                    Notification.status == NotificationStatus.FAILED,
                    Notification.attempts < notifications.MAX_ATTEMPTS,
                    Notification.deleted_at.is_(None),
                )
                .order_by(Notification.created_at)
                .limit(limit)
            )
            .scalars()
            .all()
        )

        for notification in rows:
            # Committed per notification: a batch of two hundred that failed on
            # the last one must not discard the hundred and ninety-nine sends
            # that worked — and those really were sent, so rolling back the
            # rows recording them would make the table lie.
            try:
                result = notifications.deliver(db, notification)
                db.commit()
                retried += 1
                if result.ok:
                    succeeded += 1
            except Exception:  # noqa: BLE001 - one bad row must not stop the batch
                db.rollback()
                logger.exception("Retry failed for notification %s", notification.id)

    logger.info("Notification retry: %s attempted, %s delivered", retried, succeeded)
    return {"retried": retried, "succeeded": succeeded}


def _open_breaches(db: Session, organization_id: int) -> list[BreachIncident]:
    """Breaches whose Board notification has not yet been recorded."""
    return list(
        db.execute(
            select(BreachIncident)
            .where(
                BreachIncident.organization_id == organization_id,
                BreachIncident.dpb_notified_at.is_(None),
                BreachIncident.deleted_at.is_(None),
            )
            .order_by(BreachIncident.detected_at)
        )
        .scalars()
        .all()
    )


def _check_org_breaches(db: Session, org: Organization, *, now: datetime) -> dict:
    """Warn about breaches approaching or past their 72-hour deadline.

    Each threshold fires once. Which have fired is recorded by looking for the
    notification itself rather than by a column on the incident: the send is
    the thing that must not repeat, so the record of the send is the honest
    place to ask.

    Quiet hours are *not* honoured here. A statutory 72-hour clock does not
    pause overnight, and a warning suppressed until morning could be the reason
    a fiduciary misses the window.
    """
    warned = 0

    for breach in _open_breaches(db, org.id):
        deadline = breach.dpb_deadline
        hours_remaining = (deadline - now).total_seconds() / 3600

        # Thresholds ascending: the tightest one that still describes the
        # current state is the one to send. Past the deadline, ``-1`` marks the
        # single breach-of-window notice.
        if hours_remaining < 0:
            threshold = -1
        else:
            threshold = next(
                (h for h in sorted(BREACH_WARNING_HOURS) if hours_remaining <= h), None
            )
            if threshold is None:
                continue

        if notifications.already_notified(
            db,
            organization_id=org.id,
            kind="breach_deadline",
            entity_type="breach_incident",
            entity_id=breach.id,
            reminder_offset_days=int(threshold),
        ):
            continue

        subject, body = notifications.breach_deadline_message(
            breach, hours_remaining=hours_remaining, organization_name=org.name
        )
        # Admin and Compliance Manager only. A breach notification obligation
        # rests with the fiduciary, and widening this to all staff turns a
        # confidential incident into an all-hands announcement.
        recipients = notifications.recipients_for(
            db,
            org.id,
            roles={UserRole.ADMIN, UserRole.COMPLIANCE_MANAGER},
            channels={NotificationChannel.EMAIL, NotificationChannel.IN_APP},
        )
        for recipient in recipients:
            notifications.send(
                db,
                organization_id=org.id,
                recipient=recipient,
                subject=subject,
                content=body,
                kind="breach_deadline",
                entity_type="breach_incident",
                entity_id=breach.id,
                reminder_offset_days=int(threshold),
            )

        audit_service.record(
            db,
            organization_id=org.id,
            action=AuditAction.NOTIFY,
            entity_type="breach_incident",
            entity_id=breach.id,
            actor_label=SYSTEM_ACTOR,
            summary=(
                f"DPDP notification window warning: {hours_remaining:.0f} hour(s) remaining"
            ),
            after={"hours_remaining": round(hours_remaining, 1), "threshold": threshold},
        )
        warned += 1

    db.flush()
    return {"organization_id": org.id, "warned": warned}


@celery_app.task(name="app.tasks.notification_tasks.check_breach_deadlines")
def check_breach_deadlines(organization_ids: list[int] | None = None) -> dict:
    """Every 30 minutes: warn on DPDP breach windows nearing expiry."""
    now = utcnow()
    with task_session() as db:
        orgs = active_organizations(db, organization_ids=organization_ids)
        results = per_organization(
            db,
            orgs,
            lambda session, org: _check_org_breaches(session, org, now=now),
            task_name="check_breach_deadlines",
        )
    total = sum(r.get("warned", 0) for r in results)
    logger.info("Breach deadline sweep: %s organizations, %s warnings", len(results), total)
    return {"organizations": len(results), "warnings": total}


@celery_app.task(name="app.tasks.notification_tasks.send_notification")
def send_notification(notification_id: int) -> dict:
    """Deliver one already-queued notification.

    The asynchronous path for a request handler that wants to record a
    notification inside its own transaction and not wait on SMTP. It takes an
    id rather than the content precisely so the row is committed first — a task
    handed the content could deliver a message for a transaction that was
    subsequently rolled back.
    """
    with task_session() as db:
        notification = db.get(Notification, notification_id)
        if notification is None:
            logger.warning("send_notification: no notification %s", notification_id)
            return {"notification_id": notification_id, "found": False}
        if notification.status == NotificationStatus.SENT:
            return {"notification_id": notification_id, "already_sent": True}

        result = notifications.deliver(db, notification)
        db.commit()
        return {"notification_id": notification_id, "status": str(result.status)}


@celery_app.task(name="app.tasks.notification_tasks.purge_stale_pending")
def purge_stale_pending(older_than_hours: int = 24) -> dict:
    """Mark long-PENDING notifications as failed so the retry worker sees them.

    A row stays ``PENDING`` when the process died between the insert and the
    send. Nothing else would ever look at it again: the retry worker selects
    ``FAILED``. This converts them, with the reason recorded, which is both a
    real recovery path and the only way that class of crash becomes visible.
    """
    cutoff = utcnow() - timedelta(hours=older_than_hours)
    with task_session() as db:
        rows = (
            db.execute(
                select(Notification).where(
                    Notification.status == NotificationStatus.PENDING,
                    Notification.created_at < cutoff,
                    Notification.deleted_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        for notification in rows:
            notification.status = NotificationStatus.FAILED
            notification.error = (
                f"Still pending {older_than_hours}h after creation; "
                "the sending process did not complete"
            )
        db.commit()
    logger.info("Purged %s stale pending notifications", len(rows))
    return {"converted": len(rows)}
