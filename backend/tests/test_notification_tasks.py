"""The scheduled notification work: the retry safety net and the breach clock.

Two properties carry most of this module's weight, and neither is visible from
the outside.

The first is that :func:`retry_failed` picks the right rows. It is the only
thing standing between "the mail host was down for ten minutes" and "those
reminders are gone", and the selection is easy to widen by accident into
something that burns the attempt budget of rows a retry can never help.

The second is that the breach warnings fire *once each*. ``task_acks_late``
means every one of these tasks may run twice, and a 72-hour statutory clock
that emailed the Admin every thirty minutes for three days is a clock nobody
reads by hour 12.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.audit import AuditTrail
from app.models.dpdp import BREACH_NOTIFICATION_HOURS
from app.models.enums import (
    AuditAction,
    NotificationChannel,
    NotificationStatus,
    UserRole,
)
from app.models.mixins import utcnow
from app.models.notification import Notification, NotificationPreference
from app.services import notifications
from app.tasks import notification_tasks
from app.tasks.base import SYSTEM_ACTOR
from tests.conftest import make_breach, make_user


def _queued(
    db,
    org,
    *,
    status=NotificationStatus.FAILED,
    channel=NotificationChannel.IN_APP,
    attempts: int = 1,
    **kwargs,
) -> Notification:
    """A notification already recorded, in whatever state the test needs.

    Goes through :func:`notifications.queue` rather than constructing the row,
    so a column that gains a default or a constraint is exercised here the same
    way the senders exercise it.
    """
    notification = notifications.queue(
        db,
        organization_id=org.id,
        channel=channel,
        recipient=kwargs.pop("recipient", "ops@example.com"),
        content=kwargs.pop("content", "Your GSTR-3B is due."),
        subject=kwargs.pop("subject", "Deadline"),
        **kwargs,
    )
    notification.status = status
    notification.attempts = attempts
    db.flush()
    return notification


def _notifications(db, org, *, kind=None):
    rows = db.query(Notification).filter(Notification.organization_id == org.id)
    if kind:
        rows = rows.filter(Notification.kind == kind)
    return rows.all()


def _detected_hours_ago(hours: float):
    """A ``detected_at`` that leaves ``72 - hours`` of the Board window."""
    return utcnow() - timedelta(hours=hours)


# --------------------------------------------------------------------------
# Retry
# --------------------------------------------------------------------------


class TestRetrySelection:
    """Which rows a retry pass picks up — and, more importantly, which not."""

    def test_a_failed_notification_is_retried(self, db, company):
        _queued(db, company)
        db.commit()

        assert notification_tasks.retry_failed() == {"retried": 1, "succeeded": 1}

    def test_the_retry_flips_the_row_and_counts_the_attempt(self, db, company):
        notification = _queued(db, company, attempts=1)
        db.commit()

        notification_tasks.retry_failed()

        db.expire_all()
        assert notification.status == NotificationStatus.SENT
        assert notification.attempts == 2
        assert notification.sent_at is not None

    def test_a_skipped_notification_is_left_alone(self, db, company):
        """SKIPPED means the channel has no credentials.

        Retrying it cannot succeed and would spend an attempt that the row
        needs for when the configuration is fixed.
        """
        notification = _queued(db, company, status=NotificationStatus.SKIPPED)
        db.commit()

        assert notification_tasks.retry_failed()["retried"] == 0

        db.expire_all()
        assert notification.attempts == 1

    def test_a_sent_notification_is_not_sent_again(self, db, company):
        _queued(db, company, status=NotificationStatus.SENT)
        db.commit()

        assert notification_tasks.retry_failed()["retried"] == 0

    def test_a_pending_notification_is_not_picked_up(self, db, company):
        """The gap :func:`purge_stale_pending` exists to close."""
        _queued(db, company, status=NotificationStatus.PENDING)
        db.commit()

        assert notification_tasks.retry_failed()["retried"] == 0

    def test_a_row_at_the_attempt_ceiling_is_abandoned(self, db, company):
        _queued(db, company, attempts=notifications.MAX_ATTEMPTS)
        db.commit()

        assert notification_tasks.retry_failed()["retried"] == 0

    def test_a_row_one_below_the_ceiling_still_gets_its_last_try(self, db, company):
        _queued(db, company, attempts=notifications.MAX_ATTEMPTS - 1)
        db.commit()

        assert notification_tasks.retry_failed()["retried"] == 1

    def test_a_soft_deleted_notification_is_not_retried(self, db, company):
        notification = _queued(db, company)
        notification.deleted_at = utcnow()
        db.commit()

        assert notification_tasks.retry_failed()["retried"] == 0

    def test_every_tenant_is_swept_not_just_one(self, db, company, other_company):
        """Unlike the deadline sweeps, this one is not per-organization.

        A mail outage is global, so the retry queue is too.
        """
        _queued(db, company)
        _queued(db, other_company)
        db.commit()

        assert notification_tasks.retry_failed()["retried"] == 2


class TestRetryBatching:
    def test_the_batch_is_bounded(self, db, company):
        """A host that was down overnight produces a steady catch-up.

        Not one task that tries every backlogged send inside a ten-minute time
        limit and is killed having committed none of them.
        """
        for _ in range(5):
            _queued(db, company)
        db.commit()

        assert notification_tasks.retry_failed(limit=2)["retried"] == 2

        db.expire_all()
        still_failed = [
            n for n in _notifications(db, company) if n.status == NotificationStatus.FAILED
        ]
        assert len(still_failed) == 3

    def test_the_oldest_failures_go_first(self, db, company):
        old = _queued(db, company, recipient="first@example.com")
        new = _queued(db, company, recipient="second@example.com")
        old.created_at = utcnow() - timedelta(hours=3)
        new.created_at = utcnow()
        db.commit()

        notification_tasks.retry_failed(limit=1)

        db.expire_all()
        assert old.status == NotificationStatus.SENT
        assert new.status == NotificationStatus.FAILED

    def test_successive_passes_drain_the_backlog(self, db, company):
        for _ in range(3):
            _queued(db, company)
        db.commit()

        for _ in range(3):
            notification_tasks.retry_failed(limit=1)

        db.expire_all()
        assert all(n.status == NotificationStatus.SENT for n in _notifications(db, company))

    def test_a_retry_that_fails_again_stays_failed_and_is_not_counted_delivered(
        self, db, company, monkeypatch
    ):
        _queued(db, company)
        db.commit()

        def _boom(_notification):
            raise ConnectionRefusedError("mail host still down")

        monkeypatch.setitem(notifications._TRANSPORTS, NotificationChannel.IN_APP, _boom)

        assert notification_tasks.retry_failed() == {"retried": 1, "succeeded": 0}

        db.expire_all()
        (row,) = _notifications(db, company)
        assert row.status == NotificationStatus.FAILED
        assert "mail host still down" in (row.error or "")

    def test_one_unrecoverable_row_does_not_discard_the_rest_of_the_batch(
        self, db, company, monkeypatch
    ):
        """The reason the loop commits per notification.

        ``deliver`` swallows transport errors, so a raise here stands in for
        the class of failure it cannot swallow — a row whose bookkeeping blows
        up after the message has physically gone out. Those sends really
        happened, and rolling back the rows recording them would make the table
        lie about what the customer received.
        """
        first = _queued(db, company, recipient="a@example.com")
        poisoned = _queued(db, company, recipient="poison@example.com")
        last = _queued(db, company, recipient="z@example.com")
        db.commit()

        real_deliver = notifications.deliver

        def _deliver(session, notification):
            if notification.recipient == "poison@example.com":
                raise RuntimeError("bookkeeping exploded after the send")
            return real_deliver(session, notification)

        monkeypatch.setattr(notification_tasks.notifications, "deliver", _deliver)

        assert notification_tasks.retry_failed()["retried"] == 2

        db.expire_all()
        assert first.status == NotificationStatus.SENT
        assert last.status == NotificationStatus.SENT
        assert poisoned.status == NotificationStatus.FAILED

    def test_an_unconfigured_channel_is_attempted_but_not_delivered(self, db, company):
        """Email with no SMTP host: attempted, recorded SKIPPED, not delivered.

        Which also takes it out of the retry queue on the next pass, rather
        than leaving it to consume its remaining four attempts against a
        transport the deployment does not have.
        """
        _queued(db, company, channel=NotificationChannel.EMAIL)
        db.commit()

        assert notification_tasks.retry_failed() == {"retried": 1, "succeeded": 0}

        db.expire_all()
        (row,) = _notifications(db, company)
        assert row.status == NotificationStatus.SKIPPED
        assert notification_tasks.retry_failed()["retried"] == 0


# --------------------------------------------------------------------------
# Breach deadlines
# --------------------------------------------------------------------------


class TestBreachWarningThresholds:
    def test_a_fresh_breach_with_the_whole_window_left_is_not_warned_about(
        self, db, company, company_admin
    ):
        make_breach(db, company, detected_at=_detected_hours_ago(1))

        result = notification_tasks._check_org_breaches(db, company, now=utcnow())

        assert result == {"organization_id": company.id, "warned": 0}
        assert _notifications(db, company) == []

    def test_a_breach_inside_the_first_warning_window_is_warned_about(
        self, db, company, company_admin
    ):
        make_breach(db, company, detected_at=_detected_hours_ago(60))

        assert notification_tasks._check_org_breaches(db, company, now=utcnow())["warned"] == 1

        sent = _notifications(db, company, kind="breach_deadline")
        assert {n.reminder_offset_days for n in sent} == {24}

    def test_the_tightest_matching_threshold_is_the_one_that_fires(
        self, db, company, company_admin
    ):
        """A breach logged with four hours left gets the 6-hour warning.

        Not the 24-hour one as well. Firing every threshold the clock has
        already passed would greet a late entry with a burst of warnings that
        all say something different about the same deadline.
        """
        make_breach(db, company, detected_at=_detected_hours_ago(68))

        notification_tasks._check_org_breaches(db, company, now=utcnow())

        sent = _notifications(db, company, kind="breach_deadline")
        assert {n.reminder_offset_days for n in sent} == {6}

    def test_a_missed_window_produces_the_breach_of_window_notice(
        self, db, company, company_admin
    ):
        make_breach(db, company, detected_at=_detected_hours_ago(80))

        notification_tasks._check_org_breaches(db, company, now=utcnow())

        sent = _notifications(db, company, kind="breach_deadline")
        assert {n.reminder_offset_days for n in sent} == {-1}
        assert "PASSED" in sent[0].subject

    def test_the_warning_names_how_much_of_the_window_is_left(
        self, db, company, company_admin
    ):
        make_breach(db, company, detected_at=_detected_hours_ago(60))

        notification_tasks._check_org_breaches(db, company, now=utcnow())

        (subject, *_) = {n.subject for n in _notifications(db, company)}
        assert "expires in 12 hour(s)" in subject

    @pytest.mark.parametrize("threshold", notification_tasks.BREACH_WARNING_HOURS)
    def test_every_declared_threshold_fires_at_its_own_boundary(
        self, db, company, company_admin, threshold
    ):
        """Parametrised over the constant rather than over 24 and 6.

        Adding a third warning to ``BREACH_WARNING_HOURS`` should not be
        something the suite stays silent about — the selection sorts the
        thresholds itself, so a new one that never fires would otherwise look
        exactly like a working one.
        """
        elapsed = BREACH_NOTIFICATION_HOURS - (threshold - 0.5)
        make_breach(db, company, detected_at=_detected_hours_ago(elapsed))

        notification_tasks._check_org_breaches(db, company, now=utcnow())

        offsets = {n.reminder_offset_days for n in _notifications(db, company)}
        assert offsets == {int(threshold)}


class TestBreachWarningIdempotence:
    def test_running_the_check_twice_warns_once(self, db, company, company_admin):
        make_breach(db, company, detected_at=_detected_hours_ago(60))

        notification_tasks._check_org_breaches(db, company, now=utcnow())
        before = len(_notifications(db, company))
        second = notification_tasks._check_org_breaches(db, company, now=utcnow())

        assert second["warned"] == 0
        assert len(_notifications(db, company)) == before

    def test_the_second_threshold_still_fires_as_the_clock_runs_down(
        self, db, company, company_admin
    ):
        """Each threshold fires once — but each of them does fire."""
        breach = make_breach(db, company, detected_at=_detected_hours_ago(60))

        notification_tasks._check_org_breaches(db, company, now=utcnow())
        breach.detected_at = _detected_hours_ago(68)
        db.flush()
        notification_tasks._check_org_breaches(db, company, now=utcnow())

        offsets = {n.reminder_offset_days for n in _notifications(db, company)}
        assert offsets == {24, 6}

    def test_a_breach_already_reported_to_the_board_is_dropped(
        self, db, company, company_admin
    ):
        make_breach(
            db,
            company,
            detected_at=_detected_hours_ago(80),
            dpb_notified_at=utcnow(),
        )

        assert notification_tasks._check_org_breaches(db, company, now=utcnow())["warned"] == 0

    def test_a_soft_deleted_breach_is_dropped(self, db, company, company_admin):
        breach = make_breach(db, company, detected_at=_detected_hours_ago(80))
        breach.deleted_at = utcnow()
        db.flush()

        assert notification_tasks._check_org_breaches(db, company, now=utcnow())["warned"] == 0

    def test_a_failed_warning_is_not_treated_as_already_sent(
        self, db, company, company_admin, monkeypatch
    ):
        """A send that failed must be retried, not suppressed by its own
        failure — otherwise a transport blip permanently silences a statutory
        warning."""

        def _boom(_notification):
            raise ConnectionRefusedError("down")

        monkeypatch.setitem(notifications._TRANSPORTS, NotificationChannel.IN_APP, _boom)
        monkeypatch.setitem(notifications._TRANSPORTS, NotificationChannel.EMAIL, _boom)
        make_breach(db, company, detected_at=_detected_hours_ago(60))

        notification_tasks._check_org_breaches(db, company, now=utcnow())
        assert all(n.status == NotificationStatus.FAILED for n in _notifications(db, company))

        monkeypatch.undo()
        assert notification_tasks._check_org_breaches(db, company, now=utcnow())["warned"] == 1


class TestBreachWarningAudience:
    def test_the_admin_is_told_on_email_and_in_app(self, db, company, company_admin):
        make_breach(db, company, detected_at=_detected_hours_ago(60))

        notification_tasks._check_org_breaches(db, company, now=utcnow())

        channels = {n.channel for n in _notifications(db, company)}
        assert channels == {NotificationChannel.EMAIL, NotificationChannel.IN_APP}

    def test_the_compliance_manager_is_told_too(self, db, company):
        manager = make_user(db, company, role=UserRole.COMPLIANCE_MANAGER)
        make_breach(db, company, detected_at=_detected_hours_ago(60))

        notification_tasks._check_org_breaches(db, company, now=utcnow())

        assert {n.user_id for n in _notifications(db, company)} == {manager.id}

    def test_staff_are_not_told(self, db, company, company_staff, company_reader):
        """The obligation rests with the fiduciary.

        Widening this to everyone turns a confidential incident into an
        all-hands announcement, which is its own disclosure.
        """
        make_breach(db, company, detected_at=_detected_hours_ago(60))

        notification_tasks._check_org_breaches(db, company, now=utcnow())

        assert _notifications(db, company) == []

    def test_a_deactivated_admin_is_not_written_to(self, db, company, company_admin):
        company_admin.is_active = False
        db.flush()
        make_breach(db, company, detected_at=_detected_hours_ago(60))

        notification_tasks._check_org_breaches(db, company, now=utcnow())

        assert _notifications(db, company) == []

    def test_quiet_hours_do_not_hold_a_statutory_warning_back(
        self, db, company, company_admin
    ):
        """A 72-hour clock does not pause overnight.

        The window is computed from the current IST hour so the test asserts
        the intended behaviour whatever time the suite runs at.
        """
        hour = utcnow().astimezone(notifications.IST).hour
        db.add(
            NotificationPreference(
                organization_id=company.id,
                quiet_hours_start=hour,
                quiet_hours_end=(hour + 1) % 24,
            )
        )
        db.flush()
        pref = notifications.preferences_for(db, company.id)
        assert notifications.in_quiet_hours(pref)  # guard: the window really is on

        make_breach(db, company, detected_at=_detected_hours_ago(60))

        assert notification_tasks._check_org_breaches(db, company, now=utcnow())["warned"] == 1

    def test_another_tenants_breach_produces_nothing(
        self, db, company, company_admin, other_company, other_admin
    ):
        make_breach(db, other_company, detected_at=_detected_hours_ago(60))

        assert notification_tasks._check_org_breaches(db, company, now=utcnow())["warned"] == 0
        assert _notifications(db, company) == []


class TestBreachWarningAudit:
    def _entries(self, db, org):
        return (
            db.query(AuditTrail)
            .filter(
                AuditTrail.organization_id == org.id,
                AuditTrail.entity_type == "breach_incident",
            )
            .all()
        )

    def test_the_warning_is_recorded_against_the_scheduler_not_a_person(
        self, db, company, company_admin
    ):
        make_breach(db, company, detected_at=_detected_hours_ago(60))

        notification_tasks._check_org_breaches(db, company, now=utcnow())

        (entry,) = self._entries(db, company)
        assert entry.action == AuditAction.NOTIFY
        assert entry.actor_label == SYSTEM_ACTOR
        assert entry.user_id is None

    def test_the_entry_says_how_much_of_the_window_was_left(
        self, db, company, company_admin
    ):
        make_breach(db, company, detected_at=_detected_hours_ago(60))

        notification_tasks._check_org_breaches(db, company, now=utcnow())

        (entry,) = self._entries(db, company)
        assert "12 hour(s) remaining" in entry.summary
        assert entry.after_json["threshold"] == 24

    def test_one_entry_per_warning_not_one_per_recipient(self, db, company, company_admin):
        make_user(db, company, role=UserRole.COMPLIANCE_MANAGER)
        make_breach(db, company, detected_at=_detected_hours_ago(60))

        notification_tasks._check_org_breaches(db, company, now=utcnow())

        assert len(_notifications(db, company)) == 4  # 2 people x 2 channels
        assert len(self._entries(db, company)) == 1


# --------------------------------------------------------------------------
# Task entry points
# --------------------------------------------------------------------------


class TestBreachSweepEntryPoint:
    def test_the_sweep_visits_every_active_organization(
        self, db, company, company_admin, other_company, other_admin
    ):
        make_breach(db, company, detected_at=_detected_hours_ago(60))
        make_breach(db, other_company, detected_at=_detected_hours_ago(60))
        db.commit()

        result = notification_tasks.check_breach_deadlines()

        assert result == {"organizations": 2, "warnings": 2}

    def test_an_inactive_organization_is_skipped(self, db, company, company_admin):
        make_breach(db, company, detected_at=_detected_hours_ago(60))
        company.is_active = False
        db.commit()

        assert notification_tasks.check_breach_deadlines() == {
            "organizations": 0,
            "warnings": 0,
        }

    def test_the_sweep_can_be_pointed_at_one_organization(
        self, db, company, company_admin, other_company, other_admin
    ):
        make_breach(db, company, detected_at=_detected_hours_ago(60))
        make_breach(db, other_company, detected_at=_detected_hours_ago(60))
        db.commit()

        result = notification_tasks.check_breach_deadlines(organization_ids=[company.id])

        assert result["organizations"] == 1
        db.expire_all()
        assert _notifications(db, other_company) == []

    def test_one_tenants_failure_does_not_stop_the_others(
        self, db, company, company_admin, other_company, other_admin, monkeypatch
    ):
        make_breach(db, company, detected_at=_detected_hours_ago(60))
        make_breach(db, other_company, detected_at=_detected_hours_ago(60))
        db.commit()

        real = notification_tasks._check_org_breaches

        def _explode(session, org, *, now):
            if org.id == company.id:
                raise RuntimeError("malformed tenant")
            return real(session, org, now=now)

        monkeypatch.setattr(notification_tasks, "_check_org_breaches", _explode)

        result = notification_tasks.check_breach_deadlines()

        assert result["warnings"] == 1
        db.expire_all()
        assert _notifications(db, company) == []
        assert _notifications(db, other_company) != []


class TestSendOneNotification:
    def test_a_queued_notification_is_delivered(self, db, company):
        notification = _queued(db, company, status=NotificationStatus.PENDING, attempts=0)
        db.commit()

        result = notification_tasks.send_notification(notification.id)

        assert result["notification_id"] == notification.id
        db.expire_all()
        assert notification.status == NotificationStatus.SENT

    def test_an_id_that_does_not_exist_is_reported_not_raised(self, db, company):
        """The row may have been hard-deleted between enqueue and execution.

        A raise here would retry the task forever against an id that will never
        resolve.
        """
        assert notification_tasks.send_notification(9999) == {
            "notification_id": 9999,
            "found": False,
        }

    def test_an_already_sent_notification_is_not_sent_twice(self, db, company):
        """``task_acks_late`` means this task may be redelivered.

        The guard is what stops a redelivery becoming a duplicate message.
        """
        notification = _queued(db, company, status=NotificationStatus.SENT, attempts=1)
        db.commit()

        result = notification_tasks.send_notification(notification.id)

        assert result == {"notification_id": notification.id, "already_sent": True}
        db.expire_all()
        assert notification.attempts == 1

    def test_a_failed_notification_is_attempted_again(self, db, company):
        """Unlike SENT, FAILED is not a terminal state for this task."""
        notification = _queued(db, company, status=NotificationStatus.FAILED, attempts=1)
        db.commit()

        notification_tasks.send_notification(notification.id)

        db.expire_all()
        assert notification.status == NotificationStatus.SENT
        assert notification.attempts == 2

    def test_a_transport_failure_is_recorded_rather_than_raised(
        self, db, company, monkeypatch
    ):
        notification = _queued(db, company, status=NotificationStatus.PENDING, attempts=0)
        db.commit()

        def _boom(_notification):
            raise TimeoutError("provider timed out")

        monkeypatch.setitem(notifications._TRANSPORTS, NotificationChannel.IN_APP, _boom)

        result = notification_tasks.send_notification(notification.id)

        assert "failed" in result["status"].lower()
        db.expire_all()
        assert notification.status == NotificationStatus.FAILED


class TestPurgeStalePending:
    def _pending(self, db, org, *, age_hours: float):
        notification = _queued(db, org, status=NotificationStatus.PENDING, attempts=0)
        notification.created_at = utcnow() - timedelta(hours=age_hours)
        db.flush()
        return notification

    def test_a_long_pending_row_becomes_failed(self, db, company):
        notification = self._pending(db, company, age_hours=30)
        db.commit()

        assert notification_tasks.purge_stale_pending() == {"converted": 1}

        db.expire_all()
        assert notification.status == NotificationStatus.FAILED

    def test_the_reason_is_recorded_so_the_crash_is_visible(self, db, company):
        notification = self._pending(db, company, age_hours=30)
        db.commit()

        notification_tasks.purge_stale_pending()

        db.expire_all()
        assert "did not complete" in (notification.error or "")

    def test_a_recently_queued_row_is_left_pending(self, db, company):
        """Something may still be about to send it."""
        notification = self._pending(db, company, age_hours=1)
        db.commit()

        assert notification_tasks.purge_stale_pending() == {"converted": 0}

        db.expire_all()
        assert notification.status == NotificationStatus.PENDING

    def test_the_cutoff_is_settable(self, db, company):
        notification = self._pending(db, company, age_hours=3)
        db.commit()

        assert notification_tasks.purge_stale_pending(older_than_hours=2)["converted"] == 1

        db.expire_all()
        assert notification.status == NotificationStatus.FAILED

    @pytest.mark.parametrize(
        "status",
        [NotificationStatus.SENT, NotificationStatus.FAILED, NotificationStatus.SKIPPED],
    )
    def test_a_settled_row_is_not_touched(self, db, company, status):
        notification = _queued(db, company, status=status)
        notification.created_at = utcnow() - timedelta(hours=30)
        db.commit()

        assert notification_tasks.purge_stale_pending()["converted"] == 0

        db.expire_all()
        assert notification.status == status

    def test_a_soft_deleted_row_is_not_touched(self, db, company):
        notification = self._pending(db, company, age_hours=30)
        notification.deleted_at = utcnow()
        db.commit()

        assert notification_tasks.purge_stale_pending()["converted"] == 0

    def test_the_converted_row_is_then_picked_up_by_the_retry_worker(self, db, company):
        """The whole point of the task.

        A PENDING row is invisible to every other part of the system; this is
        what puts it back on a path that ends in either a send or a recorded
        give-up.
        """
        notification = self._pending(db, company, age_hours=30)
        db.commit()

        notification_tasks.purge_stale_pending()
        assert notification_tasks.retry_failed()["succeeded"] == 1

        db.expire_all()
        assert notification.status == NotificationStatus.SENT
