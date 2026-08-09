"""Notification preferences, recipient resolution, and delivery bookkeeping.

The properties worth pinning down here are the ones that decide whether a
person is told about a deadline at all: whose preference wins, whether a
channel is on, and — the one most likely to be broken by a refactor — that a
transport failure is *recorded* rather than raised into the sweep that was
sending it.
"""
from __future__ import annotations

import smtplib
from datetime import UTC, date, datetime

import pytest

from app.core.config import settings
from app.models.enums import (
    FilingStatus,
    NotificationChannel,
    NotificationStatus,
    Regulation,
    UserRole,
)
from app.models.notification import NotificationPreference
from app.services import notifications
from tests.conftest import make_user


def _pref(db, org, *, user_id=None, **kwargs) -> NotificationPreference:
    pref = NotificationPreference(organization_id=org.id, user_id=user_id, **kwargs)
    db.add(pref)
    db.flush()
    return pref


class _Filing:
    """A stand-in for a Filing, for the message-composition tests.

    A real Filing needs an obligation, an organization and a period; none of
    that is what the wording of a reminder depends on.
    """

    def __init__(self, **kwargs):
        self.filing_type = kwargs.get("filing_type", "GSTR-3B")
        self.regulation = kwargs.get("regulation", Regulation.GST)
        self.period_key = kwargs.get("period_key", "2026-07")
        self.due_date = kwargs.get("due_date", date(2026, 8, 20))
        self.extended_due_date = kwargs.get("extended_due_date")
        self.status = kwargs.get("status", FilingStatus.NOT_STARTED)
        self.tax_payable_paise = kwargs.get("tax_payable_paise")

    @property
    def effective_due_date(self):
        return self.extended_due_date or self.due_date


class TestQuietHours:
    def test_no_preference_means_no_quiet_hours(self):
        assert not notifications.in_quiet_hours(None)

    def test_a_daytime_window_is_read_literally(self, db, company):
        pref = _pref(db, company, quiet_hours_start=13, quiet_hours_end=15)
        # 14:00 IST is 08:30 UTC.
        inside = datetime(2026, 8, 8, 8, 30, tzinfo=UTC)
        outside = datetime(2026, 8, 8, 4, 0, tzinfo=UTC)  # 09:30 IST
        assert notifications.in_quiet_hours(pref, now=inside)
        assert not notifications.in_quiet_hours(pref, now=outside)

    def test_a_window_that_wraps_midnight_still_binds(self, db, company):
        """22:00-07:00 is the setting people actually choose.

        The naive ``start <= hour < end`` reading makes this window empty,
        which silently disables quiet hours for everyone sensible.
        """
        pref = _pref(db, company, quiet_hours_start=22, quiet_hours_end=7)

        # 23:00 IST = 17:30 UTC, and 03:00 IST = 21:30 UTC the day before.
        assert notifications.in_quiet_hours(pref, now=datetime(2026, 8, 8, 17, 30, tzinfo=UTC))
        assert notifications.in_quiet_hours(pref, now=datetime(2026, 8, 7, 21, 30, tzinfo=UTC))
        # 12:00 IST = 06:30 UTC is outside it.
        assert not notifications.in_quiet_hours(pref, now=datetime(2026, 8, 8, 6, 30, tzinfo=UTC))

    def test_a_zero_width_window_is_read_as_off_not_always(self, db, company):
        """The alternative reading stops every notification the product sends."""
        pref = _pref(db, company, quiet_hours_start=9, quiet_hours_end=9)
        assert not notifications.in_quiet_hours(pref, now=datetime(2026, 8, 8, 3, 30, tzinfo=UTC))

    def test_quiet_hours_are_evaluated_in_ist(self, db, company):
        """A UTC reading would put the whole window on the wrong hours."""
        pref = _pref(db, company, quiet_hours_start=22, quiet_hours_end=7)
        # 22:00 UTC is 03:30 IST — quiet. Read as UTC it would also be quiet,
        # so the discriminating case is 18:00 UTC = 23:30 IST: quiet in IST,
        # not quiet read as UTC.
        assert notifications.in_quiet_hours(pref, now=datetime(2026, 8, 8, 18, 0, tzinfo=UTC))


class TestReminderOffsets:
    def test_absent_preference_takes_the_deployment_default(self, db, company):
        assert notifications.reminder_offsets_for(db, company.id) == settings.reminder_offsets

    def test_an_organization_override_wins_and_is_sorted_descending(self, db, company):
        _pref(db, company, reminder_offsets_json=[3, 30, 7])
        assert notifications.reminder_offsets_for(db, company.id) == [30, 7, 3]

    def test_an_empty_override_falls_back_rather_than_disabling_reminders(self, db, company):
        """``[]`` is far more likely a bug than "never remind me"."""
        _pref(db, company, reminder_offsets_json=[])
        assert notifications.reminder_offsets_for(db, company.id) == settings.reminder_offsets


class TestPreferenceResolution:
    def test_a_personal_row_beats_the_organization_default(self, db, company, company_admin):
        _pref(db, company, email_enabled=False)
        _pref(db, company, user_id=company_admin.id, email_enabled=True)

        governing = notifications.preferences_for(db, company.id, user_id=company_admin.id)
        assert governing is not None
        assert governing.email_enabled

    def test_a_user_without_a_personal_row_takes_the_org_default(
        self, db, company, company_admin, company_staff
    ):
        _pref(db, company, email_enabled=False)
        _pref(db, company, user_id=company_admin.id, email_enabled=True)

        governing = notifications.preferences_for(db, company.id, user_id=company_staff.id)
        assert governing is not None
        assert not governing.email_enabled


class TestRecipients:
    def test_roles_filter_who_is_reached(self, db, company, company_admin, company_staff):
        admins = notifications.recipients_for(
            db, company.id, roles={UserRole.ADMIN}, channels={NotificationChannel.EMAIL}
        )
        assert [r.address for r in admins] == [company_admin.email]

    def test_another_tenants_users_are_never_recipients(
        self, db, company, company_admin, other_company, other_admin
    ):
        ours = notifications.recipients_for(db, company.id, channels={NotificationChannel.EMAIL})
        assert other_admin.email not in [r.address for r in ours]

    def test_a_disabled_channel_drops_that_user_from_it(self, db, company, company_admin):
        _pref(db, company, user_id=company_admin.id, email_enabled=False)
        emails = notifications.recipients_for(
            db, company.id, channels={NotificationChannel.EMAIL}
        )
        assert company_admin.email not in [r.address for r in emails]

    def test_one_persons_opt_out_does_not_opt_out_their_colleagues(
        self, db, company, company_admin, company_staff
    ):
        _pref(db, company, user_id=company_admin.id, email_enabled=False)
        emails = notifications.recipients_for(
            db, company.id, channels={NotificationChannel.EMAIL}
        )
        assert [r.address for r in emails] == [company_staff.email]

    def test_a_shared_mailbox_is_written_to_once(self, db, company, company_admin):
        """Two people behind compliance@ get one message, not two identical ones."""
        second = make_user(db, company, role=UserRole.STAFF)
        _pref(db, company, user_id=company_admin.id, email_address="compliance@acme.test")
        _pref(db, company, user_id=second.id, email_address="COMPLIANCE@acme.test")

        emails = notifications.recipients_for(
            db, company.id, channels={NotificationChannel.EMAIL}
        )
        assert [r.address.lower() for r in emails] == ["compliance@acme.test"]

    def test_a_channel_with_no_address_is_skipped_rather_than_sent_blank(
        self, db, company, company_admin
    ):
        """Nobody has a phone number set, so WhatsApp has nowhere to go."""
        assert company_admin.phone is None
        recipients = notifications.recipients_for(
            db, company.id, channels={NotificationChannel.WHATSAPP}
        )
        assert recipients == []

    def test_a_deactivated_user_stops_being_notified(self, db, company, company_admin):
        company_admin.is_active = False
        db.flush()
        assert notifications.recipients_for(db, company.id) == []

    def test_a_soft_deleted_user_stops_being_notified(self, db, company, company_admin):
        company_admin.soft_delete()
        db.flush()
        assert notifications.recipients_for(db, company.id) == []


class TestDelivery:
    def test_in_app_delivery_marks_the_row_sent(self, db, company, company_admin):
        recipient = notifications.Recipient(
            NotificationChannel.IN_APP, company_admin.email, company_admin.id
        )
        notification = notifications.send(
            db,
            organization_id=company.id,
            recipient=recipient,
            content="Your GSTR-3B is due",
            kind="deadline_reminder",
        )
        assert notification.status == NotificationStatus.SENT
        assert notification.sent_at is not None
        assert notification.attempts == 1

    def test_an_unconfigured_channel_is_skipped_not_failed(self, db, company, company_admin):
        """SKIPPED is what keeps the retry worker off something it cannot fix."""
        assert not settings.smtp_host
        notification = notifications.send(
            db,
            organization_id=company.id,
            recipient=notifications.Recipient(
                NotificationChannel.EMAIL, company_admin.email, company_admin.id
            ),
            content="body",
        )
        assert notification.status == NotificationStatus.SKIPPED
        assert notification.sent_at is None
        assert "not configured" in (notification.error or "")

    def test_a_transport_error_is_recorded_not_raised(
        self, db, company, company_admin, monkeypatch
    ):
        """The sweep that sent this has already done the work it was reminding about."""
        monkeypatch.setattr(settings, "smtp_host", "smtp.invalid")

        def _explode(*args, **kwargs):
            raise smtplib.SMTPConnectError(421, "no")

        monkeypatch.setattr(smtplib, "SMTP", _explode)

        notification = notifications.send(
            db,
            organization_id=company.id,
            recipient=notifications.Recipient(
                NotificationChannel.EMAIL, company_admin.email, company_admin.id
            ),
            content="body",
        )
        assert notification.status == NotificationStatus.FAILED
        assert notification.error
        assert notification.attempts == 1

    def test_a_retry_increments_attempts_rather_than_resetting_them(
        self, db, company, company_admin, monkeypatch
    ):
        monkeypatch.setattr(settings, "smtp_host", "smtp.invalid")
        monkeypatch.setattr(
            smtplib, "SMTP", lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        )
        notification = notifications.queue(
            db,
            organization_id=company.id,
            channel=NotificationChannel.EMAIL,
            recipient=company_admin.email,
            content="body",
        )
        notifications.deliver(db, notification)
        notifications.deliver(db, notification)
        assert notification.attempts == 2

    def test_the_row_exists_before_the_send_is_attempted(self, db, company, company_admin):
        """A table written only on success cannot show a send that crashed."""
        notification = notifications.queue(
            db,
            organization_id=company.id,
            channel=NotificationChannel.EMAIL,
            recipient=company_admin.email,
            content="body",
        )
        assert notification.id is not None
        assert notification.status == NotificationStatus.PENDING
        assert notification.attempts == 0


class TestAlreadyNotified:
    def _queue(self, db, company, *, status, offset=None):
        notification = notifications.queue(
            db,
            organization_id=company.id,
            channel=NotificationChannel.IN_APP,
            recipient="someone@example.test",
            content="body",
            kind="breach_deadline",
            entity_type="breach_incident",
            entity_id=7,
            reminder_offset_days=offset,
        )
        notification.status = status
        db.flush()
        return notification

    def _asked(self, db, company, *, offset=None):
        return notifications.already_notified(
            db,
            organization_id=company.id,
            kind="breach_deadline",
            entity_type="breach_incident",
            entity_id=7,
            reminder_offset_days=offset,
        )

    def test_a_sent_notification_counts(self, db, company):
        self._queue(db, company, status=NotificationStatus.SENT)
        assert self._asked(db, company)

    def test_a_failed_notification_does_not_count(self, db, company):
        """Otherwise a send is suppressed forever by its own failure."""
        self._queue(db, company, status=NotificationStatus.FAILED)
        assert not self._asked(db, company)

    def test_offsets_are_tracked_separately(self, db, company):
        self._queue(db, company, status=NotificationStatus.SENT, offset=24)
        assert self._asked(db, company, offset=24)
        assert not self._asked(db, company, offset=6)

    def test_another_tenants_notification_does_not_suppress_ours(
        self, db, company, other_company
    ):
        self._queue(db, other_company, status=NotificationStatus.SENT)
        assert not self._asked(db, company)


class TestMessageComposition:
    def test_a_future_deadline_reads_as_days_remaining(self):
        subject, body = notifications.deadline_reminder_message(
            _Filing(), days_until_due=7, organization_name="Acme"
        )
        assert "due in 7 day(s)" in subject
        assert "GSTR-3B" in subject
        assert "2026-08-20" in body

    def test_due_today_says_so_rather_than_in_zero_days(self):
        subject, _ = notifications.deadline_reminder_message(
            _Filing(), days_until_due=0, organization_name="Acme"
        )
        assert "DUE TODAY" in subject

    def test_an_overdue_deadline_reads_as_overdue(self):
        subject, _ = notifications.deadline_reminder_message(
            _Filing(), days_until_due=-3, organization_name="Acme"
        )
        assert "OVERDUE by 3 day(s)" in subject

    def test_an_extension_is_described_by_the_date_that_now_binds(self):
        """And says what the original was, because that is the audit question."""
        filing = _Filing(extended_due_date=date(2026, 9, 10))
        _, body = notifications.deadline_reminder_message(
            filing, days_until_due=5, organization_name="Acme"
        )
        assert "Due date: 2026-09-10" in body
        assert "2026-08-20" in body

    def test_an_escalation_names_its_level_and_how_late_it_is(self):
        subject, body = notifications.escalation_message(
            _Filing(), days_overdue=9, level=2, organization_name="Acme"
        )
        assert "ESCALATION L2" in subject
        assert "9 day(s) overdue" in subject
        assert "level 2" in body


class TestTodayIst:
    def test_todays_date_is_the_indian_one(self, monkeypatch):
        """Just after 18:30 UTC it is already tomorrow in India.

        A sweep comparing an IST statutory deadline against a UTC date sends
        "1 day left" on the wrong day around month boundaries.
        """
        monkeypatch.setattr(
            notifications, "utcnow", lambda: datetime(2026, 8, 8, 19, 0, tzinfo=UTC)
        )
        assert notifications.today_ist() == date(2026, 8, 9)


@pytest.mark.parametrize(
    ("paise", "expected"),
    [(None, ""), (0, ""), (100, "₹1.00"), (12_345_678, "₹123,456.78")],
)
def test_rupee_formatting(paise, expected):
    assert notifications._rupees(paise) == expected
