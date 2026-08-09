"""The scheduled deadline work: reminders, escalation, generation.

These tasks run unattended and ``task_acks_late`` means any of them may run
twice. Idempotence is therefore not a nicety here — a sweep that re-sent every
reminder on each run would train every user to filter the sender, which costs
more than the missed deadline it was trying to prevent. Most of what follows is
an assertion that running the same sweep again does nothing.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.models.enums import FilingStatus, NotificationStatus, UserRole
from app.models.notification import Notification, NotificationPreference
from app.services.notifications import IST
from app.tasks import deadline_tasks
from tests.conftest import make_deadline, make_filing, make_obligation, make_user

TODAY = date(2026, 8, 8)


def at_ist(hour: int) -> datetime:
    """*hour* o'clock on TODAY, in IST.

    Quiet hours are the one part of these tasks that depends on the time and
    not only the date, so the tests that touch them hand the sweep an instant
    instead of letting it read the clock.
    """
    return datetime(TODAY.year, TODAY.month, TODAY.day, hour, tzinfo=IST)


def _quiet(db, org, *, start: int, end: int) -> NotificationPreference:
    pref = NotificationPreference(
        organization_id=org.id, quiet_hours_start=start, quiet_hours_end=end
    )
    db.add(pref)
    db.flush()
    return pref


def _notifications(db, org, *, kind=None):
    rows = db.query(Notification).filter(Notification.organization_id == org.id)
    if kind:
        rows = rows.filter(Notification.kind == kind)
    return rows.all()


def _arrange(db, org, *, due_in_days: int, status=FilingStatus.NOT_STARTED):
    """A filing due *due_in_days* from TODAY, with its deadline row."""
    obligation = make_obligation(db)
    filing = make_filing(
        db,
        org,
        obligation,
        due_date=TODAY + timedelta(days=due_in_days),
        status=status,
    )
    return filing, make_deadline(db, filing)


class TestReminderSweep:
    def test_a_deadline_inside_an_offset_sends_one_reminder(
        self, db, company, company_admin
    ):
        _arrange(db, company, due_in_days=7)

        result = deadline_tasks._sweep_org_reminders(db, company, today=TODAY)

        assert result["sent"] == 1
        sent = _notifications(db, company, kind="deadline_reminder")
        assert len(sent) == 2  # email + in_app for the one admin
        assert all(n.reminder_offset_days == 7 for n in sent)

    def test_a_deadline_far_out_sends_nothing(self, db, company, company_admin):
        _arrange(db, company, due_in_days=90)
        assert deadline_tasks._sweep_org_reminders(db, company, today=TODAY)["sent"] == 0
        assert _notifications(db, company) == []

    def test_running_the_sweep_twice_sends_one_reminder(self, db, company, company_admin):
        """The property the whole module is built around."""
        _arrange(db, company, due_in_days=7)

        deadline_tasks._sweep_org_reminders(db, company, today=TODAY)
        before = len(_notifications(db, company))
        deadline_tasks._sweep_org_reminders(db, company, today=TODAY)

        assert len(_notifications(db, company)) == before

    def test_only_the_nearest_due_offset_fires_not_every_passed_one(
        self, db, company, company_admin
    ):
        """A filing created three days before its deadline gets one reminder.

        Not the 30-, 15- and 7-day ones all at once, which is what a naive
        "send every offset that has passed" would do.
        """
        _arrange(db, company, due_in_days=3)

        deadline_tasks._sweep_org_reminders(db, company, today=TODAY)

        offsets = {n.reminder_offset_days for n in _notifications(db, company)}
        assert offsets == {3}

    def test_successive_offsets_fire_as_the_deadline_approaches(
        self, db, company, company_admin
    ):
        filing, deadline = _arrange(db, company, due_in_days=30)

        deadline_tasks._sweep_org_reminders(db, company, today=TODAY)
        deadline_tasks._sweep_org_reminders(db, company, today=TODAY + timedelta(days=15))
        deadline_tasks._sweep_org_reminders(db, company, today=TODAY + timedelta(days=23))

        assert sorted(deadline.reminders_sent) == [7, 15, 30]

    def test_a_satisfied_deadline_stops_being_chased(self, db, company, company_admin):
        _, deadline = _arrange(db, company, due_in_days=7)
        deadline.is_satisfied = True
        db.flush()

        assert deadline_tasks._sweep_org_reminders(db, company, today=TODAY)["sent"] == 0

    def test_a_soft_deleted_filing_stops_being_chased(self, db, company, company_admin):
        filing, _ = _arrange(db, company, due_in_days=7)
        filing.soft_delete()
        db.flush()

        assert deadline_tasks._sweep_org_reminders(db, company, today=TODAY)["sent"] == 0

    def test_quiet_hours_suppress_the_whole_sweep(self, db, company, company_admin):
        """Nothing goes out at 2am, and the sweep says why.

        The hour is passed in rather than left to the wall clock. A sweep test
        that reads the real time passes on 23 runs out of 24 and fails on the
        one at the edge of the window, which is a test that reports a bug the
        day nobody is looking at CI.
        """
        _quiet(db, company, start=22, end=7)
        _arrange(db, company, due_in_days=7)
        db.flush()

        result = deadline_tasks._sweep_org_reminders(
            db, company, today=TODAY, now=at_ist(2)
        )

        assert result["skipped_quiet_hours"]
        assert _notifications(db, company) == []

    def test_outside_the_window_the_sweep_runs(self, db, company, company_admin):
        """The other half of the same setting.

        Without this, quiet hours that had been misread as "always" would look
        correct: the suppression test would pass and no reminder would ever be
        sent.
        """
        _quiet(db, company, start=22, end=7)
        _arrange(db, company, due_in_days=7)
        db.flush()

        result = deadline_tasks._sweep_org_reminders(
            db, company, today=TODAY, now=at_ist(11)
        )

        assert result["sent"] == 1
        assert "skipped_quiet_hours" not in result

    @pytest.mark.parametrize(
        ("hour", "quiet"),
        [(21, False), (22, True), (6, True), (7, False)],
        ids=["the hour before", "the first hour", "the last hour", "the hour after"],
    )
    def test_the_window_is_half_open_at_both_ends(
        self, db, company, company_admin, hour, quiet
    ):
        """A 22–7 window is nine hours, not eight or ten.

        The wrapping comparison is the easiest thing in this module to get off
        by one, and either error is invisible in production: an hour too few
        wakes somebody at 6am, an hour too many silently holds a reminder that
        was due at 7.
        """
        _quiet(db, company, start=22, end=7)
        _arrange(db, company, due_in_days=7)
        db.flush()

        result = deadline_tasks._sweep_org_reminders(
            db, company, today=TODAY, now=at_ist(hour)
        )

        assert result.get("skipped_quiet_hours", False) is quiet

    def test_a_suppressed_reminder_is_sent_by_the_next_sweep(
        self, db, company, company_admin
    ):
        """Quiet hours defer, they do not cancel.

        The offset must not be marked sent on the way past — a reminder
        swallowed because the sweep happened to land at 3am is one nobody ever
        finds out about.
        """
        _quiet(db, company, start=22, end=7)
        _, deadline = _arrange(db, company, due_in_days=7)
        db.flush()

        deadline_tasks._sweep_org_reminders(db, company, today=TODAY, now=at_ist(3))
        assert deadline.reminders_sent == []

        result = deadline_tasks._sweep_org_reminders(
            db, company, today=TODAY, now=at_ist(9)
        )
        assert result["sent"] == 1

    def test_an_offset_is_recorded_even_with_no_recipients(self, db, company):
        """An org with nobody configured must not replay its whole schedule
        the moment a first user is added."""
        _, deadline = _arrange(db, company, due_in_days=7)

        deadline_tasks._sweep_org_reminders(db, company, today=TODAY)

        # The 30- and 15-day offsets are recorded alongside the 7 that fired:
        # their moment passed unsent, and re-sending them once the deadline is
        # closer would be backwards.
        assert sorted(deadline.reminders_sent) == [7, 15, 30]
        assert deadline.last_reminder_at is not None

    def test_a_wide_offset_never_fires_after_a_tighter_one(self, db, company, company_admin):
        """A filing first seen three days out gets one reminder, not five.

        Selecting the widest matching band instead would send the 30-day
        reminder now, the 15-day one on the next hourly sweep and the 7-day one
        after that — five emails in five hours for a single deadline.
        """
        _arrange(db, company, due_in_days=3)

        for _ in range(4):
            deadline_tasks._sweep_org_reminders(db, company, today=TODAY)

        offsets = [n.reminder_offset_days for n in _notifications(db, company)]
        assert set(offsets) == {3}

    def test_an_organization_override_replaces_the_default_schedule(
        self, db, company, company_admin
    ):
        from app.models.notification import NotificationPreference

        db.add(
            NotificationPreference(
                organization_id=company.id, reminder_offsets_json=[10]
            )
        )
        _arrange(db, company, due_in_days=9)
        db.flush()

        deadline_tasks._sweep_org_reminders(db, company, today=TODAY)

        assert {n.reminder_offset_days for n in _notifications(db, company)} == {10}

    def test_one_tenants_deadline_never_reaches_another(
        self, db, company, other_company, other_admin
    ):
        _arrange(db, company, due_in_days=7)

        deadline_tasks._sweep_org_reminders(db, company, today=TODAY)

        assert _notifications(db, other_company) == []

    def test_an_overdue_deadline_still_reminds(self, db, company, company_admin):
        """Past the due date every band qualifies, so the tightest one fires."""
        _arrange(db, company, due_in_days=-5)

        result = deadline_tasks._sweep_org_reminders(db, company, today=TODAY)

        assert result["sent"] == 1
        assert "OVERDUE" in (_notifications(db, company)[0].subject or "")

    def test_an_overdue_deadline_reminds_only_once(self, db, company, company_admin):
        """Chasing an overdue filing is the escalation task's job, not this one's."""
        _arrange(db, company, due_in_days=-5)

        deadline_tasks._sweep_org_reminders(db, company, today=TODAY)
        deadline_tasks._sweep_org_reminders(db, company, today=TODAY + timedelta(days=1))
        deadline_tasks._sweep_org_reminders(db, company, today=TODAY + timedelta(days=2))

        assert len(_notifications(db, company, kind="deadline_reminder")) == 2


class TestEscalation:
    def test_a_filing_not_yet_due_is_not_escalated(self, db, company, company_admin):
        _, deadline = _arrange(db, company, due_in_days=3)

        assert deadline_tasks._escalate_org(db, company, today=TODAY)["escalated"] == 0
        assert deadline.escalation_level == 0

    def test_one_day_overdue_reaches_level_one(self, db, company, company_admin):
        _, deadline = _arrange(db, company, due_in_days=-1)

        assert deadline_tasks._escalate_org(db, company, today=TODAY)["escalated"] == 1
        assert deadline.escalation_level == 1

    def test_a_week_overdue_reaches_level_two(self, db, company, company_admin):
        _, deadline = _arrange(db, company, due_in_days=-8)

        deadline_tasks._escalate_org(db, company, today=TODAY)

        assert deadline.escalation_level == 2

    def test_a_month_overdue_reaches_level_three(self, db, company, company_admin):
        _, deadline = _arrange(db, company, due_in_days=-31)

        deadline_tasks._escalate_org(db, company, today=TODAY)

        assert deadline.escalation_level == 3

    def test_escalation_notifies_only_when_the_level_changes(
        self, db, company, company_admin
    ):
        """Otherwise a filing 30 days overdue emails the same people daily,
        and by Friday they have filtered the sender."""
        _arrange(db, company, due_in_days=-2)

        deadline_tasks._escalate_org(db, company, today=TODAY)
        after_first = len(_notifications(db, company, kind="escalation"))
        deadline_tasks._escalate_org(db, company, today=TODAY)

        assert after_first > 0
        assert len(_notifications(db, company, kind="escalation")) == after_first

    def test_a_level_change_notifies_again(self, db, company, company_admin):
        filing, deadline = _arrange(db, company, due_in_days=-1)

        deadline_tasks._escalate_org(db, company, today=TODAY)
        first = len(_notifications(db, company, kind="escalation"))
        deadline_tasks._escalate_org(db, company, today=TODAY + timedelta(days=7))

        assert deadline.escalation_level == 2
        assert len(_notifications(db, company, kind="escalation")) > first

    def test_level_two_widens_the_audience_to_the_admin(self, db, company):
        """The point of an escalation is that more senior people learn of it."""
        staff = make_user(db, company, role=UserRole.STAFF)
        admin = make_user(db, company, role=UserRole.ADMIN)
        _arrange(db, company, due_in_days=-8)

        deadline_tasks._escalate_org(db, company, today=TODAY)

        reached = {n.recipient for n in _notifications(db, company, kind="escalation")}
        assert admin.email in reached
        assert staff.email in reached

    def test_level_one_does_not_yet_reach_the_admin(self, db, company):
        staff = make_user(db, company, role=UserRole.STAFF)
        admin = make_user(db, company, role=UserRole.ADMIN)
        _arrange(db, company, due_in_days=-1)

        deadline_tasks._escalate_org(db, company, today=TODAY)

        reached = {n.recipient for n in _notifications(db, company, kind="escalation")}
        assert staff.email in reached
        assert admin.email not in reached

    def test_escalation_writes_to_the_audit_trail(self, db, company, company_admin):
        from app.models.audit import AuditTrail

        _arrange(db, company, due_in_days=-1)

        deadline_tasks._escalate_org(db, company, today=TODAY)

        entries = db.query(AuditTrail).filter_by(organization_id=company.id).all()
        assert any("Escalated to level 1" in (e.summary or "") for e in entries)

    def test_a_satisfied_deadline_is_never_escalated(self, db, company, company_admin):
        _, deadline = _arrange(db, company, due_in_days=-40)
        deadline.is_satisfied = True
        db.flush()

        assert deadline_tasks._escalate_org(db, company, today=TODAY)["escalated"] == 0
        assert deadline.escalation_level == 0


class TestEscalationLevels:
    def test_thresholds_map_days_overdue_to_levels(self):
        assert deadline_tasks._escalation_level_for(0) == 0
        assert deadline_tasks._escalation_level_for(1) == 1
        assert deadline_tasks._escalation_level_for(6) == 1
        assert deadline_tasks._escalation_level_for(7) == 2
        assert deadline_tasks._escalation_level_for(29) == 2
        assert deadline_tasks._escalation_level_for(30) == 3
        assert deadline_tasks._escalation_level_for(400) == 3

    def test_the_audience_only_ever_widens(self):
        """A level that dropped someone would mean escalating *away* from
        the person doing the work."""
        audience = deadline_tasks.ESCALATION_AUDIENCE
        levels = sorted(audience)
        for lower, higher in zip(levels, levels[1:], strict=False):
            assert audience[lower] <= audience[higher]


class TestTaskEntryPoints:
    """The Celery bodies, run eagerly, over a committed database."""

    def test_the_reminder_task_sweeps_every_active_organization(
        self, db, company, company_admin, other_company, other_admin
    ):
        _arrange(db, company, due_in_days=7)
        _arrange(db, other_company, due_in_days=7)
        db.commit()

        result = deadline_tasks.sweep_deadline_reminders()

        assert result["organizations"] == 2
        assert result["reminders_sent"] == 2

    def test_an_inactive_organization_is_skipped(self, db, company, company_admin):
        _arrange(db, company, due_in_days=7)
        company.is_active = False
        db.commit()

        result = deadline_tasks.sweep_deadline_reminders()

        assert result["organizations"] == 0
        assert result["reminders_sent"] == 0

    def test_the_task_can_be_pointed_at_one_organization(
        self, db, company, company_admin, other_company, other_admin
    ):
        _arrange(db, company, due_in_days=7)
        _arrange(db, other_company, due_in_days=7)
        db.commit()

        result = deadline_tasks.sweep_deadline_reminders(organization_ids=[company.id])

        assert result["organizations"] == 1
        assert _notifications(db, other_company) == []

    def test_the_escalation_task_runs_over_every_organization(
        self, db, company, company_admin
    ):
        _arrange(db, company, due_in_days=-10)
        db.commit()

        assert deadline_tasks.escalate_overdue()["escalated"] == 1

    def test_a_failing_organization_does_not_stop_the_sweep(
        self, db, company, company_admin, other_company, other_admin, monkeypatch
    ):
        """One tenant's bad data costs that tenant's iteration, not the run."""
        _arrange(db, company, due_in_days=7)
        _arrange(db, other_company, due_in_days=7)
        db.commit()

        original = deadline_tasks._sweep_org_reminders
        seen: list[int] = []

        def _explode_on_first(session, org, **kwargs):
            seen.append(org.id)
            if org.id == company.id:
                raise RuntimeError("malformed profile")
            return original(session, org, **kwargs)

        monkeypatch.setattr(deadline_tasks, "_sweep_org_reminders", _explode_on_first)

        result = deadline_tasks.sweep_deadline_reminders()

        assert seen == [company.id, other_company.id]
        assert result["reminders_sent"] == 1
        assert len(_notifications(db, other_company)) == 2


class TestFilingGenerationTask:
    def test_generation_is_recorded_in_the_audit_trail(self, db, company, seeded):
        from app.models.audit import AuditTrail
        from app.services.applicability import sync_organization_obligations

        sync_organization_obligations(db, company, on=TODAY)
        db.commit()

        result = deadline_tasks.generate_upcoming_filings(organization_ids=[company.id])

        assert result["created"] > 0
        entries = db.query(AuditTrail).filter_by(organization_id=company.id).all()
        assert any("Scheduled generation created" in (e.summary or "") for e in entries)

    def test_running_generation_twice_creates_nothing_the_second_time(
        self, db, company, seeded
    ):
        from app.services.applicability import sync_organization_obligations

        sync_organization_obligations(db, company, on=TODAY)
        db.commit()

        first = deadline_tasks.generate_upcoming_filings(organization_ids=[company.id])
        second = deadline_tasks.generate_upcoming_filings(organization_ids=[company.id])

        assert first["created"] > 0
        assert second["created"] == 0

    def test_every_generated_filing_gets_a_deadline_row(self, db, company, seeded):
        from app.models.filing import Deadline, Filing
        from app.services.applicability import sync_organization_obligations

        sync_organization_obligations(db, company, on=TODAY)
        db.commit()
        deadline_tasks.generate_upcoming_filings(organization_ids=[company.id])

        filings = db.query(Filing).filter_by(organization_id=company.id).count()
        deadlines = db.query(Deadline).filter_by(organization_id=company.id).count()
        assert filings > 0
        assert deadlines == filings


class TestNotificationStatusOnSweep:
    def test_reminders_are_recorded_as_delivered_on_in_app(
        self, db, company, company_admin
    ):
        _arrange(db, company, due_in_days=7)

        deadline_tasks._sweep_org_reminders(db, company, today=TODAY)

        in_app = [
            n for n in _notifications(db, company) if n.channel == "in_app"
        ]
        assert in_app
        assert all(n.status == NotificationStatus.SENT for n in in_app)

    def test_an_unconfigured_email_channel_records_skipped_not_failed(
        self, db, company, company_admin
    ):
        _arrange(db, company, due_in_days=7)

        deadline_tasks._sweep_org_reminders(db, company, today=TODAY)

        emails = [n for n in _notifications(db, company) if n.channel == "email"]
        assert emails
        assert all(n.status == NotificationStatus.SKIPPED for n in emails)
