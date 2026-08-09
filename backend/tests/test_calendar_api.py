"""The compliance calendar, and the generation run that fills it.

The calendar is a union of three sources, and the reason it is not just the
filings list with a date filter is the other two: a deadline the parser pulled
out of a show-cause notice, and a DPDP breach's 72-hour Board clock. Both are
real deadlines with real consequences and neither exists in the obligations
catalogue, so most of what follows checks that each source appears, carries its
own ``source`` tag, and drops out of the window on the same rule as the others.

The window itself is tested at its edges rather than in the middle. ``start``
and ``end`` are inclusive, the test is against the *effective* due date so an
extension moves an item between windows, and a request wider than the cap is
refused rather than served slowly — that last one is the query that would hold
a connection open across a firm's whole client book.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.models.enums import FilingStatus, Regulation, UserRole
from app.models.filing import Filing
from app.models.mixins import utcnow
from tests.conftest import (
    API,
    auth,
    make_assignment,
    make_breach,
    make_document,
    make_engagement,
    make_filing,
    make_obligation,
    make_org,
    make_user,
)

TODAY = date(2026, 8, 8)
PARAMS = {"today": TODAY.isoformat()}


def get_calendar(client, user, **params):
    return client.get(
        f"{API}/calendar", headers=auth(user), params={**PARAMS, **params}
    ).json()


class TestFilingItems:
    def test_a_filing_in_the_window_appears(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(
            db, title="Monthly GST return", penalty_per_day_paise=20_000
        )
        filing = make_filing(
            db, company, obligation, due_date=TODAY + timedelta(days=12)
        )

        body = get_calendar(app_client, company_admin)

        assert body["total"] == 1
        item = body["items"][0]
        assert item["source"] == "filing"
        assert item["filing_id"] == filing.id
        assert item["title"] == "Monthly GST return"
        assert item["filing_type"] == "GSTR-3B"
        assert item["period_key"] == "2026-07"
        assert item["days_until_due"] == 12
        # 12 days out is inside the 15-day orange band, not yellow: the bands
        # are read as "up to and including", which rounds a penalty risk the
        # stricter way.
        assert item["urgency"] == "orange"
        assert item["penalty_per_day_paise"] == 20_000
        assert item["is_open"] is True

    def test_the_default_window_looks_back_a_month_and_ahead_a_quarter(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        for label, offset in (("just_inside_past", -29), ("far_past", -40)):
            make_filing(
                db,
                company,
                obligation,
                period_key=label,
                due_date=TODAY + timedelta(days=offset),
            )
        for label, offset in (("just_inside_future", 89), ("far_future", 100)):
            make_filing(
                db,
                company,
                obligation,
                period_key=label,
                due_date=TODAY + timedelta(days=offset),
            )

        keys = {i["period_key"] for i in get_calendar(app_client, company_admin)["items"]}

        assert keys == {"just_inside_past", "just_inside_future"}

    def test_the_window_bounds_are_inclusive(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(db, company, obligation, period_key="start", due_date=TODAY)
        make_filing(
            db,
            company,
            obligation,
            period_key="end",
            due_date=TODAY + timedelta(days=10),
        )

        body = get_calendar(
            app_client,
            company_admin,
            start=TODAY.isoformat(),
            end=(TODAY + timedelta(days=10)).isoformat(),
        )

        assert body["total"] == 2

    def test_an_extension_moves_the_item_into_the_window(
        self, app_client, db, company, company_admin
    ):
        make_filing(
            db,
            company,
            make_obligation(db),
            due_date=TODAY + timedelta(days=200),
            extended_due_date=TODAY + timedelta(days=5),
        )

        body = get_calendar(app_client, company_admin)

        assert body["total"] == 1
        assert body["items"][0]["date"] == (TODAY + timedelta(days=5)).isoformat()

    def test_acknowledged_filings_are_hidden_by_default(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(
            db,
            company,
            obligation,
            period_key="done",
            status=FilingStatus.ACKNOWLEDGED,
            due_date=TODAY + timedelta(days=3),
        )
        make_filing(
            db,
            company,
            obligation,
            period_key="na",
            status=FilingStatus.NOT_APPLICABLE,
            due_date=TODAY + timedelta(days=3),
        )

        assert get_calendar(app_client, company_admin)["total"] == 0
        assert get_calendar(app_client, company_admin, include_closed=True)["total"] == 2

    def test_a_submitted_filing_stays_visible(
        self, app_client, db, company, company_admin
    ):
        # Submitted but not yet acknowledged is still live work — the return
        # went in and the receipt has not come back.
        make_filing(
            db,
            company,
            make_obligation(db),
            status=FilingStatus.SUBMITTED,
            due_date=TODAY + timedelta(days=3),
        )

        assert get_calendar(app_client, company_admin)["total"] == 1

    def test_filtering_by_regulation(self, app_client, db, company, company_admin):
        gst = make_obligation(db, regulation=Regulation.GST)
        tds = make_obligation(db, regulation=Regulation.INCOME_TAX)
        make_filing(db, company, gst, due_date=TODAY + timedelta(days=3))
        make_filing(db, company, tds, due_date=TODAY + timedelta(days=4))

        body = get_calendar(app_client, company_admin, regulation="gst")

        assert body["total"] == 1
        assert body["items"][0]["regulation"] == "gst"

    def test_a_soft_deleted_filing_is_gone(
        self, app_client, db, company, company_admin
    ):
        filing = make_filing(
            db, company, make_obligation(db), due_date=TODAY + timedelta(days=3)
        )
        filing.soft_delete()
        db.flush()

        assert get_calendar(app_client, company_admin)["total"] == 0

    def test_another_tenants_filings_are_invisible(
        self, app_client, db, company, other_company, company_admin
    ):
        make_filing(
            db, other_company, make_obligation(db), due_date=TODAY + timedelta(days=3)
        )

        assert get_calendar(app_client, company_admin)["total"] == 0


class TestDocumentItems:
    def test_an_extracted_deadline_appears_as_its_own_source(
        self, app_client, db, company, company_admin
    ):
        make_document(
            db,
            company,
            title="Show-cause notice under s.73",
            regulation=Regulation.GST,
            extracted_deadline=TODAY + timedelta(days=15),
        )

        item = get_calendar(app_client, company_admin)["items"][0]

        assert item["source"] == "document"
        assert item["title"] == "Show-cause notice under s.73"
        assert item["filing_id"] is None
        assert item["days_until_due"] == 15
        assert item["urgency"] == "orange"

    def test_a_document_with_no_deadline_is_not_on_the_calendar(
        self, app_client, db, company, company_admin
    ):
        make_document(db, company)

        assert get_calendar(app_client, company_admin)["total"] == 0

    def test_a_document_deadline_outside_the_window_is_dropped(
        self, app_client, db, company, company_admin
    ):
        make_document(db, company, extracted_deadline=TODAY + timedelta(days=200))

        assert get_calendar(app_client, company_admin)["total"] == 0

    def test_the_regulation_filter_applies_to_documents_too(
        self, app_client, db, company, company_admin
    ):
        make_document(
            db,
            company,
            regulation=Regulation.INCOME_TAX,
            extracted_deadline=TODAY + timedelta(days=5),
        )

        assert get_calendar(app_client, company_admin, regulation="gst")["total"] == 0
        assert (
            get_calendar(app_client, company_admin, regulation="income_tax")["total"] == 1
        )

    def test_a_soft_deleted_document_is_gone(
        self, app_client, db, company, company_admin
    ):
        document = make_document(
            db, company, extracted_deadline=TODAY + timedelta(days=5)
        )
        document.soft_delete()
        db.flush()

        assert get_calendar(app_client, company_admin)["total"] == 0


class TestBreachItems:
    def test_an_unreported_breach_shows_its_72_hour_clock(
        self, app_client, db, company, company_admin
    ):
        make_breach(db, company, title="Backup bucket left public")

        item = get_calendar(app_client, company_admin)["items"][0]

        assert item["source"] == "breach"
        assert item["regulation"] == "dpdp"
        assert "Notify the Data Protection Board" in item["title"]
        assert "Backup bucket left public" in item["title"]

    def test_a_reported_breach_has_no_remaining_deadline(
        self, app_client, db, company, company_admin
    ):
        make_breach(db, company, dpb_notified_at=utcnow())

        assert get_calendar(app_client, company_admin)["total"] == 0

    def test_an_overdue_breach_clock_reads_as_overdue(
        self, app_client, db, company, company_admin
    ):
        # Detected 10 days ago: the Board deadline was 7 days ago, which lands
        # inside the default 30-day lookback.
        make_breach(db, company, detected_at=utcnow() - timedelta(days=10))

        item = get_calendar(app_client, company_admin)["items"][0]

        assert item["days_until_due"] < 0
        assert item["urgency"] == "overdue"

    def test_a_breach_older_than_the_lookback_falls_out(
        self, app_client, db, company, company_admin
    ):
        make_breach(db, company, detected_at=utcnow() - timedelta(days=90))

        assert get_calendar(app_client, company_admin)["total"] == 0

    def test_breaches_are_omitted_when_filtering_to_another_regulation(
        self, app_client, db, company, company_admin
    ):
        make_breach(db, company)

        assert get_calendar(app_client, company_admin, regulation="gst")["total"] == 0
        assert get_calendar(app_client, company_admin, regulation="dpdp")["total"] == 1


class TestAggregates:
    def test_the_sources_are_summed_into_one_calendar(
        self, app_client, db, company, company_admin
    ):
        make_filing(
            db, company, make_obligation(db), due_date=TODAY + timedelta(days=3)
        )
        make_document(db, company, extracted_deadline=TODAY + timedelta(days=4))
        make_breach(db, company)

        body = get_calendar(app_client, company_admin)

        assert body["total"] == 3
        assert {i["source"] for i in body["items"]} == {"filing", "document", "breach"}

    def test_items_are_ordered_by_date(self, app_client, db, company, company_admin):
        obligation = make_obligation(db)
        for offset in (20, 3, 11):
            make_filing(
                db,
                company,
                obligation,
                period_key=f"p{offset}",
                due_date=TODAY + timedelta(days=offset),
            )

        dates = [i["date"] for i in get_calendar(app_client, company_admin)["items"]]

        assert dates == sorted(dates)

    def test_overdue_counts_only_open_items(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(
            db,
            company,
            obligation,
            period_key="open",
            due_date=TODAY - timedelta(days=5),
        )
        make_filing(
            db,
            company,
            obligation,
            period_key="filed",
            due_date=TODAY - timedelta(days=5),
            status=FilingStatus.LATE_FILED,
        )

        body = get_calendar(app_client, company_admin)

        assert body["total"] == 2
        assert body["overdue"] == 1

    def test_due_this_week_spans_seven_days_from_the_reference(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        for offset in (0, 7, 8):
            make_filing(
                db,
                company,
                obligation,
                period_key=f"p{offset}",
                due_date=TODAY + timedelta(days=offset),
            )

        assert get_calendar(app_client, company_admin)["due_this_week"] == 2

    def test_the_breakdowns_count_every_item(
        self, app_client, db, company, company_admin
    ):
        gst = make_obligation(db, regulation=Regulation.GST)
        tds = make_obligation(db, regulation=Regulation.INCOME_TAX)
        make_filing(db, company, gst, due_date=TODAY + timedelta(days=3))
        make_filing(db, company, tds, due_date=TODAY + timedelta(days=40))

        body = get_calendar(app_client, company_admin)

        assert body["by_regulation"] == {"gst": 1, "income_tax": 1}
        assert body["by_urgency"] == {"red": 1, "green": 1}


class TestWindowValidation:
    def test_an_inverted_window_is_refused(self, app_client, company_admin):
        response = app_client.get(
            f"{API}/calendar",
            headers=auth(company_admin),
            params={"start": "2026-09-01", "end": "2026-08-01"},
        )

        assert response.status_code == 422
        assert "ends before it starts" in response.json()["error"]["message"]

    def test_a_window_past_the_cap_is_refused_with_the_size_it_asked_for(
        self, app_client, company_admin
    ):
        response = app_client.get(
            f"{API}/calendar",
            headers=auth(company_admin),
            params={"start": "2020-01-01", "end": "2026-01-01"},
        )

        assert response.status_code == 422
        body = response.json()["error"]
        assert "at most 800 days" in body["message"]
        assert body["details"]["requested_days"] == 2192

    def test_a_window_at_the_cap_is_allowed(self, app_client, company_admin):
        response = app_client.get(
            f"{API}/calendar",
            headers=auth(company_admin),
            params={
                "start": TODAY.isoformat(),
                "end": (TODAY + timedelta(days=800)).isoformat(),
            },
        )

        assert response.status_code == 200


class TestConsolidatedCalendar:
    def test_a_company_may_not_ask_for_one(self, app_client, company_admin):
        response = app_client.get(
            f"{API}/calendar", headers=auth(company_admin), params={"all_clients": True}
        )

        assert response.status_code == 422
        assert "CA firm" in response.json()["error"]["message"]

    def test_a_firm_sees_every_client_and_itself(
        self, app_client, db, ca_firm, company, firm_admin, engagement
    ):
        obligation = make_obligation(db)
        make_filing(db, company, obligation, due_date=TODAY + timedelta(days=3))
        make_filing(
            db, ca_firm, obligation, period_key="own", due_date=TODAY + timedelta(days=4)
        )

        body = get_calendar(app_client, firm_admin, all_clients=True)

        assert body["total"] == 2
        assert {i["organization_name"] for i in body["items"]} == {
            company.name,
            ca_firm.name,
        }

    def test_without_the_flag_a_firm_sees_only_its_own(
        self, app_client, db, ca_firm, company, firm_admin, engagement
    ):
        make_filing(
            db, company, make_obligation(db), due_date=TODAY + timedelta(days=3)
        )

        assert get_calendar(app_client, firm_admin)["total"] == 0

    def test_staff_get_only_their_assigned_clients(
        self, app_client, db, ca_firm, firm_staff
    ):
        obligation = make_obligation(db)
        mine = make_org(db, name="Assigned Ltd")
        theirs = make_org(db, name="Unassigned Ltd")
        assigned = make_engagement(db, ca_firm, mine)
        make_engagement(db, ca_firm, theirs)
        make_assignment(db, firm_staff, assigned)
        make_filing(db, mine, obligation, due_date=TODAY + timedelta(days=3))
        make_filing(
            db, theirs, obligation, period_key="x", due_date=TODAY + timedelta(days=3)
        )

        body = get_calendar(app_client, firm_staff, all_clients=True)

        assert [i["organization_name"] for i in body["items"]] == ["Assigned Ltd"]

    def test_a_firm_user_acting_for_one_client_sees_that_client(
        self, app_client, db, ca_firm, company, firm_admin, engagement
    ):
        make_filing(
            db, company, make_obligation(db), due_date=TODAY + timedelta(days=3)
        )

        body = app_client.get(
            f"{API}/calendar",
            headers=auth(firm_admin, client_org_id=company.id),
            params=PARAMS,
        ).json()

        assert body["total"] == 1
        assert body["items"][0]["organization_id"] == company.id


class TestCalendarAccess:
    def test_it_needs_a_token(self, app_client):
        assert app_client.get(f"{API}/calendar").status_code == 401

    def test_a_read_only_user_may_read_it(
        self, app_client, db, company, company_reader
    ):
        make_filing(
            db, company, make_obligation(db), due_date=TODAY + timedelta(days=3)
        )

        assert get_calendar(app_client, company_reader)["total"] == 1


class TestGeneration:
    def test_it_creates_the_filings_an_organization_owes(
        self, app_client, db, seeded, company, company_admin
    ):
        from app.services.applicability import sync_organization_obligations

        sync_organization_obligations(db, company, on=TODAY)
        db.flush()

        body = app_client.post(
            f"{API}/calendar/generate", headers=auth(company_admin), params=PARAMS
        ).json()

        assert body["organization_id"] == company.id
        assert body["obligations_considered"] > 0
        assert body["created"] > 0
        assert db.query(Filing).filter_by(organization_id=company.id).count() == body["created"]

    def test_running_it_twice_creates_nothing_the_second_time(
        self, app_client, db, seeded, company, company_admin
    ):
        from app.services.applicability import sync_organization_obligations

        sync_organization_obligations(db, company, on=TODAY)
        db.flush()

        first = app_client.post(
            f"{API}/calendar/generate", headers=auth(company_admin), params=PARAMS
        ).json()
        second = app_client.post(
            f"{API}/calendar/generate", headers=auth(company_admin), params=PARAMS
        ).json()

        assert first["created"] > 0
        assert second["created"] == 0
        assert second["skipped_existing"] >= first["created"]

    def test_an_organization_with_no_applicability_run_gets_an_honest_zero(
        self, app_client, company, company_admin
    ):
        body = app_client.post(
            f"{API}/calendar/generate", headers=auth(company_admin), params=PARAMS
        ).json()

        assert body["obligations_considered"] == 0
        assert body["created"] == 0

    def test_the_horizon_bounds_how_far_ahead_it_generates(
        self, app_client, db, seeded, company, company_admin
    ):
        from app.services.applicability import sync_organization_obligations

        sync_organization_obligations(db, company, on=TODAY)
        db.flush()

        narrow = app_client.post(
            f"{API}/calendar/generate",
            headers=auth(company_admin),
            params={**PARAMS, "horizon_days": 1},
        ).json()

        assert narrow["created"] < 200

    def test_a_horizon_past_the_contract_is_refused(self, app_client, company_admin):
        response = app_client.post(
            f"{API}/calendar/generate",
            headers=auth(company_admin),
            params={"horizon_days": 500},
        )

        assert response.status_code == 422

    def test_a_read_only_user_may_not_generate(self, app_client, company_reader):
        response = app_client.post(
            f"{API}/calendar/generate", headers=auth(company_reader)
        )

        assert response.status_code == 403

    def test_staff_may_not_generate(self, app_client, company_staff):
        assert (
            app_client.post(
                f"{API}/calendar/generate", headers=auth(company_staff)
            ).status_code
            == 403
        )

    def test_a_compliance_manager_may_generate(self, app_client, db, company):
        manager = make_user(db, company, role=UserRole.COMPLIANCE_MANAGER)

        response = app_client.post(
            f"{API}/calendar/generate", headers=auth(manager), params=PARAMS
        )

        assert response.status_code == 200

    def test_a_generation_run_that_created_something_is_audited(
        self, app_client, db, seeded, company, company_admin
    ):
        from app.models.audit import AuditTrail
        from app.services.applicability import sync_organization_obligations

        sync_organization_obligations(db, company, on=TODAY)
        db.flush()

        app_client.post(
            f"{API}/calendar/generate", headers=auth(company_admin), params=PARAMS
        )

        entry = (
            db.query(AuditTrail)
            .filter_by(organization_id=company.id, entity_type="filing")
            .order_by(AuditTrail.sequence.desc())
            .first()
        )
        assert entry is not None
        assert str(entry.action) == "generate"
        assert entry.after_json["created"] > 0

    def test_a_run_that_created_nothing_is_not_audited(
        self, app_client, db, company, company_admin
    ):
        from app.models.audit import AuditTrail

        app_client.post(
            f"{API}/calendar/generate", headers=auth(company_admin), params=PARAMS
        )

        assert db.query(AuditTrail).filter_by(entity_type="filing").count() == 0

    def test_the_generated_filings_land_on_the_calendar(
        self, app_client, db, seeded, company, company_admin
    ):
        from app.services.applicability import sync_organization_obligations

        sync_organization_obligations(db, company, on=TODAY)
        db.flush()

        app_client.post(
            f"{API}/calendar/generate", headers=auth(company_admin), params=PARAMS
        )

        assert get_calendar(app_client, company_admin)["total"] > 0
