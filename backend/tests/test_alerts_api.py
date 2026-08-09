"""The alert bell: what needs attention, what was sent, and how to be reached.

Three resources under one prefix because they are one thing to the user. The
tests are organised around the three decisions in the router that are not
obvious from the endpoint names:

* **the summary is seven counts in one round trip.** The alternative is a
  header that fires seven requests on every page load and populates one badge
  at a time. Each count is pinned separately here, because a summary that
  silently returns zero for one of them looks exactly like "nothing wrong";
* **the notification history includes the failures.** A reminder that did not
  go out is the thing a client points at after a missed deadline, so a history
  showing only successes could not answer the question it exists for;
* **changing the organization's channels is a manager's decision, changing your
  own is not.** Turning every channel off for a whole tenant would stop every
  deadline reminder for every user in it, which is not something a Staff
  account should be able to do — but a user muting their own SMS affects
  nobody else.

The regulatory half is where the commercial value sits: a circular is public
and everyone can read it, but *what it means for this client* is the impact,
and acknowledging one is the record a regulator asks for afterwards.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.models.enums import (
    FilingStatus,
    ImpactLevel,
    NotificationChannel,
    NotificationStatus,
    Regulation,
    UserRole,
)
from app.models.mixins import utcnow
from app.models.notification import Notification, NotificationPreference
from tests.conftest import (
    API,
    auth,
    make_breach,
    make_dsr,
    make_filing,
    make_impact,
    make_obligation,
    make_update,
    make_user,
)

TODAY = date(2026, 8, 15)


def make_notification(
    db,
    org,
    *,
    channel: NotificationChannel = NotificationChannel.EMAIL,
    status: NotificationStatus = NotificationStatus.SENT,
    kind: str = "deadline_reminder",
    recipient: str = "priya@example.com",
    content: str = "GSTR-3B is due in three days.",
    **kwargs,
) -> Notification:
    notification = Notification(
        organization_id=org.id,
        channel=channel,
        status=status,
        kind=kind,
        recipient=recipient,
        content=content,
        **kwargs,
    )
    db.add(notification)
    db.flush()
    return notification


class TestSummary:
    def test_a_quiet_tenant_gets_zeroes_rather_than_an_empty_body(
        self, app_client, company_admin
    ):
        body = app_client.get(
            f"{API}/alerts/summary", headers=auth(company_admin)
        ).json()

        assert body == {
            "unacknowledged_impacts": 0,
            "critical_impacts": 0,
            "overdue_filings": 0,
            "due_within_7_days": 0,
            "open_breach_incidents": 0,
            "overdue_data_requests": 0,
            "failed_notifications": 0,
        }

    def test_an_overdue_filing_is_counted(self, app_client, db, company, company_admin):
        make_filing(db, company, make_obligation(db), due_date=TODAY - timedelta(days=1))

        body = app_client.get(
            f"{API}/alerts/summary", headers=auth(company_admin), params={"today": TODAY}
        ).json()

        assert body["overdue_filings"] == 1
        assert body["due_within_7_days"] == 0

    def test_an_extension_moves_a_filing_out_of_overdue(
        self, app_client, db, company, company_admin
    ):
        """The extended date is the real one once granted."""
        make_filing(
            db,
            company,
            make_obligation(db),
            due_date=TODAY - timedelta(days=5),
            extended_due_date=TODAY + timedelta(days=3),
        )

        body = app_client.get(
            f"{API}/alerts/summary", headers=auth(company_admin), params={"today": TODAY}
        ).json()

        assert body["overdue_filings"] == 0
        assert body["due_within_7_days"] == 1

    def test_the_week_window_is_inclusive_at_both_ends(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(db, company, obligation, due_date=TODAY, period_key="a")
        make_filing(db, company, obligation, due_date=TODAY + timedelta(days=7), period_key="b")
        make_filing(db, company, obligation, due_date=TODAY + timedelta(days=8), period_key="c")

        body = app_client.get(
            f"{API}/alerts/summary", headers=auth(company_admin), params={"today": TODAY}
        ).json()

        assert body["due_within_7_days"] == 2

    def test_an_acknowledged_return_is_not_counted_however_late_it_was(
        self, app_client, db, company, company_admin
    ):
        make_filing(
            db,
            company,
            make_obligation(db),
            due_date=TODAY - timedelta(days=30),
            status=FilingStatus.ACKNOWLEDGED,
        )

        body = app_client.get(
            f"{API}/alerts/summary", headers=auth(company_admin), params={"today": TODAY}
        ).json()

        assert body["overdue_filings"] == 0

    def test_a_rejected_filing_is_still_open(self, app_client, db, company, company_admin):
        """Rejected means it came back and still has to be filed."""
        make_filing(
            db,
            company,
            make_obligation(db),
            due_date=TODAY - timedelta(days=1),
            status=FilingStatus.REJECTED,
        )

        body = app_client.get(
            f"{API}/alerts/summary", headers=auth(company_admin), params={"today": TODAY}
        ).json()

        assert body["overdue_filings"] == 1

    def test_unacknowledged_impacts_are_counted_and_the_critical_ones_twice(
        self, app_client, db, company, company_admin
    ):
        make_impact(db, company, impact_level=ImpactLevel.CRITICAL)
        make_impact(db, company, impact_level=ImpactLevel.LOW)
        make_impact(db, company, impact_level=ImpactLevel.CRITICAL, is_acknowledged=True)

        body = app_client.get(
            f"{API}/alerts/summary", headers=auth(company_admin)
        ).json()

        assert body["unacknowledged_impacts"] == 2
        assert body["critical_impacts"] == 1

    def test_an_open_breach_is_counted_until_it_is_closed(
        self, app_client, db, company, company_admin
    ):
        make_breach(db, company)
        make_breach(db, company, closed_at=utcnow())

        body = app_client.get(
            f"{API}/alerts/summary", headers=auth(company_admin)
        ).json()

        assert body["open_breach_incidents"] == 1

    def test_an_overdue_data_request_is_counted_unless_it_is_finished(
        self, app_client, db, company, company_admin
    ):
        make_dsr(db, company, due_date=TODAY - timedelta(days=1))
        make_dsr(db, company, due_date=TODAY - timedelta(days=1), status="completed")
        make_dsr(db, company, due_date=TODAY - timedelta(days=1), status="rejected")
        make_dsr(db, company, due_date=TODAY + timedelta(days=5))

        body = app_client.get(
            f"{API}/alerts/summary", headers=auth(company_admin), params={"today": TODAY}
        ).json()

        assert body["overdue_data_requests"] == 1

    def test_failed_notifications_are_counted(self, app_client, db, company, company_admin):
        make_notification(db, company, status=NotificationStatus.FAILED)
        make_notification(db, company, status=NotificationStatus.SENT)
        # SKIPPED is not a failure: the deployment has no credential for that
        # channel and retrying will not help.
        make_notification(db, company, status=NotificationStatus.SKIPPED)

        body = app_client.get(
            f"{API}/alerts/summary", headers=auth(company_admin)
        ).json()

        assert body["failed_notifications"] == 1

    def test_another_tenants_trouble_is_not_counted(
        self, app_client, db, other_company, company_admin
    ):
        make_filing(
            db, other_company, make_obligation(db), due_date=TODAY - timedelta(days=1)
        )
        make_impact(db, other_company)
        make_breach(db, other_company)
        make_notification(db, other_company, status=NotificationStatus.FAILED)

        body = app_client.get(
            f"{API}/alerts/summary", headers=auth(company_admin), params={"today": TODAY}
        ).json()

        assert set(body.values()) == {0}

    def test_a_read_only_user_may_see_the_badge(self, app_client, company_reader):
        assert (
            app_client.get(f"{API}/alerts/summary", headers=auth(company_reader)).status_code
            == 200
        )

    def test_it_needs_a_token(self, app_client):
        assert app_client.get(f"{API}/alerts/summary").status_code == 401


class TestNotificationHistory:
    def test_the_failures_are_in_the_history_with_their_reason(
        self, app_client, db, company, company_admin
    ):
        """"We never got the reminder" is only answerable from this."""
        make_notification(
            db,
            company,
            status=NotificationStatus.FAILED,
            error="WhatsApp template not approved",
            attempts=3,
        )

        item = app_client.get(
            f"{API}/alerts/notifications", headers=auth(company_admin)
        ).json()["items"][0]

        assert item["status"] == "failed"
        assert item["error"] == "WhatsApp template not approved"
        assert item["attempts"] == 3

    def test_a_tenant_sees_only_their_own(
        self, app_client, db, company, other_company, company_admin
    ):
        make_notification(db, company)
        make_notification(db, other_company)

        body = app_client.get(
            f"{API}/alerts/notifications", headers=auth(company_admin)
        ).json()

        assert body["total"] == 1

    def test_filtering_by_channel_status_and_kind(
        self, app_client, db, company, company_admin
    ):
        make_notification(db, company, channel=NotificationChannel.WHATSAPP)
        make_notification(
            db, company, status=NotificationStatus.FAILED, kind="regulatory_alert"
        )

        def total(**params):
            return app_client.get(
                f"{API}/alerts/notifications", headers=auth(company_admin), params=params
            ).json()["total"]

        assert total(channel="whatsapp") == 1
        assert total(status="failed") == 1
        assert total(kind="regulatory_alert") == 1
        assert total(kind="nothing_like_this") == 0

    def test_the_newest_is_first(self, app_client, db, company, company_admin):
        make_notification(db, company, content="Older")
        make_notification(db, company, content="Newer")

        items = app_client.get(
            f"{API}/alerts/notifications", headers=auth(company_admin)
        ).json()["items"]

        assert [i["content"] for i in items] == ["Newer", "Older"]

    def test_pagination_reports_the_full_total(
        self, app_client, db, company, company_admin
    ):
        for _ in range(5):
            make_notification(db, company)

        body = app_client.get(
            f"{API}/alerts/notifications", headers=auth(company_admin), params={"limit": 2}
        ).json()

        assert len(body["items"]) == 2
        assert body["total"] == 5


class TestPreferences:
    def test_the_first_read_creates_the_row_rather_than_404ing(
        self, app_client, db, company, company_admin
    ):
        """A preferences screen that has to handle "none exist yet" has a bug."""
        body = app_client.get(
            f"{API}/alerts/preferences", headers=auth(company_admin)
        ).json()

        assert body["organization_id"] == company.id
        assert body["user_id"] is None
        assert body["email_enabled"] is True
        assert db.query(NotificationPreference).count() == 1

    def test_reading_twice_does_not_create_two_rows(
        self, app_client, db, company_admin
    ):
        first = app_client.get(f"{API}/alerts/preferences", headers=auth(company_admin))
        second = app_client.get(f"{API}/alerts/preferences", headers=auth(company_admin))

        assert first.json()["id"] == second.json()["id"]
        assert db.query(NotificationPreference).count() == 1

    def test_a_personal_override_is_a_separate_row_from_the_org_default(
        self, app_client, db, company_admin
    ):
        org_default = app_client.get(
            f"{API}/alerts/preferences", headers=auth(company_admin)
        ).json()
        mine = app_client.get(
            f"{API}/alerts/preferences", headers=auth(company_admin), params={"mine": True}
        ).json()

        assert mine["id"] != org_default["id"]
        assert mine["user_id"] == company_admin.id
        assert org_default["user_id"] is None

    def test_two_users_get_their_own_overrides(
        self, app_client, db, company_admin, company_staff
    ):
        admin = app_client.get(
            f"{API}/alerts/preferences", headers=auth(company_admin), params={"mine": True}
        ).json()
        staff = app_client.get(
            f"{API}/alerts/preferences", headers=auth(company_staff), params={"mine": True}
        ).json()

        assert admin["id"] != staff["id"]
        assert staff["user_id"] == company_staff.id

    def test_a_manager_changes_the_organization_default(
        self, app_client, db, company, company_admin
    ):
        body = app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_admin),
            json={"whatsapp_enabled": False, "email_address": "compliance@acme.example.com"},
        ).json()

        assert body["whatsapp_enabled"] is False
        assert body["email_address"] == "compliance@acme.example.com"
        assert body["user_id"] is None

    def test_staff_may_not_change_the_organization_default(
        self, app_client, company_staff
    ):
        """Muting every channel for a tenant stops every reminder for everyone."""
        response = app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_staff),
            json={"email_enabled": False},
        )

        assert response.status_code == 403
        body = response.json()["error"]
        assert "compliance_manager" in body["message"]
        assert "mine=true" in body["details"]["hint"]

    def test_staff_may_change_their_own(self, app_client, db, company_staff):
        response = app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_staff),
            params={"mine": True},
            json={"sms_enabled": True},
        )

        assert response.status_code == 200
        assert response.json()["user_id"] == company_staff.id
        assert response.json()["sms_enabled"] is True

    def test_a_read_only_user_may_not_change_even_their_own(
        self, app_client, company_reader
    ):
        response = app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_reader),
            params={"mine": True},
            json={"sms_enabled": True},
        )

        assert response.status_code == 403

    def test_a_personal_change_leaves_the_organization_default_alone(
        self, app_client, db, company_admin
    ):
        app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_admin),
            params={"mine": True},
            json={"email_enabled": False},
        )

        org_default = app_client.get(
            f"{API}/alerts/preferences", headers=auth(company_admin)
        ).json()

        assert org_default["email_enabled"] is True

    def test_a_phone_number_is_normalised_to_ten_digits(
        self, app_client, company_admin
    ):
        body = app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_admin),
            json={"whatsapp_number": "+91 98765-43210"},
        ).json()

        assert body["whatsapp_number"] == "9876543210"

    def test_an_unusable_phone_number_is_refused(self, app_client, company_admin):
        response = app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_admin),
            json={"sms_number": "12345"},
        )

        assert response.status_code == 422

    def test_reminder_offsets_are_normalised_descending_and_de_duplicated(
        self, app_client, company_admin
    ):
        """The sweep walks them furthest-first and stops at the first already
        sent, which is only correct on a sorted list."""
        body = app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_admin),
            json={"reminder_offsets_json": [3, 15, 3, 7]},
        ).json()

        assert body["reminder_offsets_json"] == [15, 7, 3]

    def test_an_absurd_reminder_offset_is_refused(self, app_client, company_admin):
        response = app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_admin),
            json={"reminder_offsets_json": [400]},
        )

        assert response.status_code == 422

    def test_quiet_hours_must_be_set_as_a_pair(self, app_client, company_admin):
        half = app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_admin),
            json={"quiet_hours_start": 22},
        )
        both = app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_admin),
            json={"quiet_hours_start": 22, "quiet_hours_end": 7},
        )

        assert half.status_code == 422
        assert both.status_code == 200

    def test_an_hour_outside_the_clock_is_refused(self, app_client, company_admin):
        response = app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_admin),
            json={"quiet_hours_start": 22, "quiet_hours_end": 24},
        )

        assert response.status_code == 422

    def test_the_change_is_audited_with_the_before_and_after(
        self, app_client, db, company_admin
    ):
        from app.models.audit import AuditTrail

        app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_admin),
            json={"email_enabled": False},
        )

        entry = (
            db.query(AuditTrail).filter_by(entity_type="notification_preference").one()
        )
        assert entry.before_json == {"email_enabled": True}
        assert entry.after_json == {"email_enabled": False}
        assert "Organization" in entry.summary

    def test_a_personal_change_says_so_in_the_summary(
        self, app_client, db, company_admin
    ):
        from app.models.audit import AuditTrail

        app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_admin),
            params={"mine": True},
            json={"email_enabled": False},
        )

        entry = (
            db.query(AuditTrail).filter_by(entity_type="notification_preference").one()
        )
        assert "Personal" in entry.summary

    def test_a_no_op_patch_writes_no_audit_entry(self, app_client, db, company_admin):
        from app.models.audit import AuditTrail

        response = app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(company_admin),
            json={"email_enabled": True},
        )

        assert response.status_code == 200
        assert (
            db.query(AuditTrail).filter_by(entity_type="notification_preference").count()
            == 0
        )

    def test_another_tenants_preferences_are_not_reachable(
        self, app_client, db, company, other_company, company_admin, other_admin
    ):
        app_client.patch(
            f"{API}/alerts/preferences",
            headers=auth(other_admin),
            json={"email_enabled": False},
        )

        mine = app_client.get(
            f"{API}/alerts/preferences", headers=auth(company_admin)
        ).json()

        assert mine["organization_id"] == company.id
        assert mine["email_enabled"] is True


class TestRegulatoryImpacts:
    def test_the_impact_carries_the_update_that_caused_it(
        self, app_client, db, company, company_admin
    ):
        update = make_update(db, title="GST return filing amended", regulation=Regulation.GST)
        make_impact(db, company, update, rationale="You file GSTR-3B monthly")

        item = app_client.get(
            f"{API}/alerts/regulatory", headers=auth(company_admin)
        ).json()["items"][0]

        assert item["rationale"] == "You file GSTR-3B monthly"
        assert item["update"]["title"] == "GST return filing amended"
        assert item["update"]["regulation"] == "gst"

    def test_the_worst_comes_first_within_a_page(
        self, app_client, db, company, company_admin
    ):
        """A CRITICAL change from last week outranks a LOW one from yesterday.

        The severity order is spelled out in the router rather than left to the
        column, because the enum's members sort alphabetically and 'critical'
        would fall between 'none' and 'high' rather than above them.
        """
        for level in (ImpactLevel.LOW, ImpactLevel.CRITICAL, ImpactLevel.MEDIUM, ImpactLevel.HIGH):
            make_impact(db, company, impact_level=level)

        items = app_client.get(
            f"{API}/alerts/regulatory", headers=auth(company_admin)
        ).json()["items"]

        assert [i["impact_level"] for i in items] == [
            "critical",
            "high",
            "medium",
            "low",
        ]

    def test_filtering_by_level_and_on_the_unacknowledged(
        self, app_client, db, company, company_admin
    ):
        make_impact(db, company, impact_level=ImpactLevel.CRITICAL)
        make_impact(db, company, impact_level=ImpactLevel.LOW, is_acknowledged=True)

        def total(**params):
            return app_client.get(
                f"{API}/alerts/regulatory", headers=auth(company_admin), params=params
            ).json()["total"]

        assert total(impact_level="critical") == 1
        assert total(unacknowledged_only=True) == 1

    def test_another_tenants_impacts_are_invisible(
        self, app_client, db, other_company, company_admin
    ):
        """The circular is public; what it means for a client is not."""
        make_impact(db, other_company)

        body = app_client.get(
            f"{API}/alerts/regulatory", headers=auth(company_admin)
        ).json()

        assert body["total"] == 0


class TestAcknowledgement:
    def test_a_manager_acknowledges_and_is_named_on_the_record(
        self, app_client, db, company, company_admin
    ):
        impact = make_impact(db, company)

        body = app_client.post(
            f"{API}/alerts/regulatory/{impact.id}/acknowledge",
            headers=auth(company_admin),
            json={"notes": "Reviewed; no change needed to our filing calendar"},
        ).json()

        assert body["is_acknowledged"] is True
        assert body["acknowledged_by_id"] == company_admin.id
        assert body["acknowledged_at"] is not None

    def test_acknowledging_twice_keeps_the_first_signature(
        self, app_client, db, company, company_admin
    ):
        """The record is who first accepted it, not who clicked last."""
        impact = make_impact(db, company)
        manager = make_user(db, company, role=UserRole.COMPLIANCE_MANAGER)

        first = app_client.post(
            f"{API}/alerts/regulatory/{impact.id}/acknowledge",
            headers=auth(company_admin),
            json={},
        ).json()
        second = app_client.post(
            f"{API}/alerts/regulatory/{impact.id}/acknowledge",
            headers=auth(manager),
            json={},
        ).json()

        assert second["acknowledged_by_id"] == company_admin.id
        assert second["acknowledged_at"] == first["acknowledged_at"]

    def test_it_is_audited_with_the_notes(self, app_client, db, company, company_admin):
        from app.models.audit import AuditTrail

        impact = make_impact(db, company)

        app_client.post(
            f"{API}/alerts/regulatory/{impact.id}/acknowledge",
            headers=auth(company_admin),
            json={"notes": "Discussed with the client on 14 August"},
        )

        entry = db.query(AuditTrail).filter_by(entity_type="regulatory_impact").one()
        assert entry.after_json["is_acknowledged"] is True
        assert entry.after_json["notes"] == "Discussed with the client on 14 August"

    def test_a_second_acknowledgement_writes_no_second_entry(
        self, app_client, db, company, company_admin
    ):
        from app.models.audit import AuditTrail

        impact = make_impact(db, company)

        for _ in range(2):
            app_client.post(
                f"{API}/alerts/regulatory/{impact.id}/acknowledge",
                headers=auth(company_admin),
                json={},
            )

        assert db.query(AuditTrail).filter_by(entity_type="regulatory_impact").count() == 1

    def test_staff_may_not_acknowledge(self, app_client, db, company, company_staff):
        """It is the record that somebody competent read the circular."""
        impact = make_impact(db, company)

        response = app_client.post(
            f"{API}/alerts/regulatory/{impact.id}/acknowledge",
            headers=auth(company_staff),
            json={},
        )

        assert response.status_code == 403

    def test_another_tenants_impact_is_a_404(
        self, app_client, db, other_company, company_admin
    ):
        theirs = make_impact(db, other_company)

        response = app_client.post(
            f"{API}/alerts/regulatory/{theirs.id}/acknowledge",
            headers=auth(company_admin),
            json={},
        )

        assert response.status_code == 404


class TestRegulatoryFeed:
    def test_the_feed_is_shared_rather_than_tenant_scoped(
        self, app_client, db, company_admin, other_admin
    ):
        """A circular is public. It is the impact that is private."""
        make_update(db, title="CBIC circular 210/2026")

        mine = app_client.get(
            f"{API}/alerts/regulatory/updates", headers=auth(company_admin)
        ).json()
        theirs = app_client.get(
            f"{API}/alerts/regulatory/updates", headers=auth(other_admin)
        ).json()

        assert mine["total"] == 1
        assert mine["total"] == theirs["total"]

    def test_an_unpublished_update_is_withheld(self, app_client, db, company_admin):
        """Showing it would be showing raw scraped text with no summary."""
        make_update(db, is_published=False)

        body = app_client.get(
            f"{API}/alerts/regulatory/updates", headers=auth(company_admin)
        ).json()

        assert body["total"] == 0

    def test_a_soft_deleted_update_is_withheld(self, app_client, db, company_admin):
        update = make_update(db)
        update.soft_delete()
        db.flush()

        body = app_client.get(
            f"{API}/alerts/regulatory/updates", headers=auth(company_admin)
        ).json()

        assert body["total"] == 0

    def test_the_newest_is_first(self, app_client, db, company_admin):
        make_update(db, title="Older", published_date=date(2026, 7, 1))
        make_update(db, title="Newer", published_date=date(2026, 8, 1))

        items = app_client.get(
            f"{API}/alerts/regulatory/updates", headers=auth(company_admin)
        ).json()["items"]

        assert [i["title"] for i in items] == ["Newer", "Older"]

    def test_the_feed_can_be_bounded_by_date(self, app_client, db, company_admin):
        make_update(db, title="Old", published_date=date(2026, 6, 1))
        make_update(db, title="Recent", published_date=date(2026, 8, 1))

        items = app_client.get(
            f"{API}/alerts/regulatory/updates",
            headers=auth(company_admin),
            params={"since": "2026-07-01"},
        ).json()["items"]

        assert [i["title"] for i in items] == ["Recent"]

    def test_pagination_reports_the_full_total(self, app_client, db, company_admin):
        for n in range(5):
            make_update(db, title=f"Circular {n}")

        body = app_client.get(
            f"{API}/alerts/regulatory/updates",
            headers=auth(company_admin),
            params={"limit": 2},
        ).json()

        assert len(body["items"]) == 2
        assert body["total"] == 5
