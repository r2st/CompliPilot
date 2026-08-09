"""The dashboards, and the compliance score they are built around.

The score is the product's headline number, and the thing worth testing about
it is not the arithmetic but the *ordering* the arithmetic is supposed to
produce. A CA opens the firm dashboard to answer "who do I call today", and the
answer is wrong if a client with one overdue return outranks a client with six.
So the tests here assert relationships — overdue costs more than late, the
attention queue is sorted worst-first — rather than pinning the constants,
which are tuning knobs and should be free to move.

Two properties get their own tests because they are silent when they break:

* penalty exposure is capped at the statutory maximum, so a two-year-old return
  reports the ₹10,000 the Act allows rather than the ₹146,000 an uncapped
  ₹200/day accumulation would invent;
* the firm dashboard's client set comes from the assignment rules, so a Staff
  member's consolidated view shows their clients and not the whole book — the
  same rule the client list applies, which is exactly why it could be dropped
  here without anything else failing.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.models.enums import FilingStatus, UserRole
from app.models.mixins import utcnow
from app.routers.dashboard import band_for
from tests.conftest import (
    API,
    auth,
    make_assignment,
    make_breach,
    make_document,
    make_dsr,
    make_engagement,
    make_filing,
    make_impact,
    make_obligation,
    make_org,
    make_user,
)

TODAY = date(2026, 8, 8)
PARAMS = {"today": TODAY.isoformat()}


def get_dashboard(client, user, **params):
    return client.get(
        f"{API}/dashboard", headers=auth(user), params={**PARAMS, **params}
    ).json()


class TestComplianceScore:
    def test_an_organization_with_nothing_tracked_scores_100(
        self, app_client, company_admin
    ):
        body = get_dashboard(app_client, company_admin)

        assert body["score"]["score"] == 100
        assert body["score"]["band"] == "excellent"
        assert body["score"]["total_filings"] == 0

    def test_everything_filed_on_time_still_scores_100(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        for period in ("2026-05", "2026-06", "2026-07"):
            make_filing(
                db,
                company,
                obligation,
                period_key=period,
                due_date=date(2026, 6, 20),
                status=FilingStatus.ACKNOWLEDGED,
            )

        score = get_dashboard(app_client, company_admin)["score"]

        assert score["score"] == 100
        assert score["on_time_filings"] == 3
        assert score["open_filings"] == 0

    def test_an_overdue_filing_costs_more_than_a_late_one(
        self, app_client, db, company, other_company, company_admin, other_admin
    ):
        obligation = make_obligation(db)
        make_filing(db, company, obligation, due_date=TODAY - timedelta(days=5))
        make_filing(
            db,
            other_company,
            obligation,
            due_date=TODAY - timedelta(days=5),
            status=FilingStatus.LATE_FILED,
        )

        overdue = get_dashboard(app_client, company_admin)["score"]
        late = get_dashboard(app_client, other_admin)["score"]

        assert overdue["overdue_filings"] == 1
        assert late["late_filings"] == 1
        # The relationship, not the constants: a return still open and accruing
        # must always cost more than one that was filed and is finished.
        assert overdue["score"] < late["score"] < 100

    def test_a_filing_due_today_is_not_yet_overdue(
        self, app_client, db, company, company_admin
    ):
        make_filing(db, company, make_obligation(db), due_date=TODAY)

        score = get_dashboard(app_client, company_admin)["score"]

        assert score["overdue_filings"] == 0
        assert score["open_filings"] == 1
        assert score["score"] == 100

    def test_an_extension_pushes_a_filing_out_of_overdue(
        self, app_client, db, company, company_admin
    ):
        make_filing(
            db,
            company,
            make_obligation(db),
            due_date=TODAY - timedelta(days=10),
            extended_due_date=TODAY + timedelta(days=10),
        )

        assert get_dashboard(app_client, company_admin)["score"]["overdue_filings"] == 0

    def test_an_unacknowledged_impact_costs_points(
        self, app_client, db, company, company_admin
    ):
        make_impact(db, company)

        score = get_dashboard(app_client, company_admin)["score"]

        assert score["unacknowledged_impacts"] == 1
        assert score["score"] < 100

    def test_an_acknowledged_impact_costs_nothing(
        self, app_client, db, company, company_admin
    ):
        make_impact(db, company, is_acknowledged=True)

        score = get_dashboard(app_client, company_admin)["score"]

        assert score["unacknowledged_impacts"] == 0
        assert score["score"] == 100

    def test_a_breach_past_its_72_hours_is_the_heaviest_penalty(
        self, app_client, db, company, company_admin
    ):
        make_breach(db, company, detected_at=utcnow() - timedelta(hours=100))

        score = get_dashboard(app_client, company_admin)["score"]

        assert score["overdue_dpb_notifications"] == 1
        # Heavier than an overdue filing: missing the Board's window is a
        # statutory violation, not an accruing fee.
        assert score["score"] <= 100 - 15

    def test_a_breach_inside_its_window_is_not_counted(
        self, app_client, db, company, company_admin
    ):
        make_breach(db, company, detected_at=utcnow() - timedelta(hours=10))

        score = get_dashboard(app_client, company_admin)["score"]

        assert score["overdue_dpb_notifications"] == 0
        assert score["score"] == 100

    def test_a_notified_breach_is_not_counted_however_late(
        self, app_client, db, company, company_admin
    ):
        make_breach(
            db,
            company,
            detected_at=utcnow() - timedelta(days=30),
            dpb_notified_at=utcnow(),
        )

        assert (
            get_dashboard(app_client, company_admin)["score"][
                "overdue_dpb_notifications"
            ]
            == 0
        )

    def test_the_score_floors_at_zero(self, app_client, db, company, company_admin):
        obligation = make_obligation(db)
        for n in range(30):
            make_filing(
                db,
                company,
                obligation,
                period_key=f"2025-{n:02d}",
                due_date=TODAY - timedelta(days=60),
            )

        score = get_dashboard(app_client, company_admin)["score"]

        assert score["overdue_filings"] == 30
        assert score["score"] == 0
        assert score["band"] == "critical"

    def test_a_soft_deleted_filing_leaves_the_score(
        self, app_client, db, company, company_admin
    ):
        filing = make_filing(
            db, company, make_obligation(db), due_date=TODAY - timedelta(days=30)
        )
        assert get_dashboard(app_client, company_admin)["score"]["score"] < 100

        filing.soft_delete()
        db.flush()

        assert get_dashboard(app_client, company_admin)["score"]["score"] == 100


class TestBands:
    def test_the_bands_partition_the_whole_range(self):
        assert [band_for(s) for s in (100, 90, 89, 75, 74, 50, 49, 0)] == [
            "excellent",
            "excellent",
            "good",
            "good",
            "at_risk",
            "at_risk",
            "critical",
            "critical",
        ]


class TestPenaltyExposure:
    def test_exposure_accrues_per_day_overdue(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db, penalty_per_day_paise=20_000)
        make_filing(db, company, obligation, due_date=TODAY - timedelta(days=10))

        body = get_dashboard(app_client, company_admin)

        assert body["penalty_exposure_paise"] == 200_000

    def test_exposure_stops_at_the_statutory_cap(
        self, app_client, db, company, company_admin
    ):
        # ₹200/day against a ₹10,000 cap, 700 days overdue. Uncapped this would
        # report ₹140,000 of exposure the Act cannot impose.
        obligation = make_obligation(
            db, penalty_per_day_paise=20_000, penalty_max_paise=1_000_000
        )
        make_filing(db, company, obligation, due_date=TODAY - timedelta(days=700))

        body = get_dashboard(app_client, company_admin)

        assert body["penalty_exposure_paise"] == 1_000_000

    def test_an_obligation_with_no_penalty_contributes_nothing(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db, penalty_per_day_paise=None)
        make_filing(db, company, obligation, due_date=TODAY - timedelta(days=10))

        assert get_dashboard(app_client, company_admin)["penalty_exposure_paise"] == 0

    def test_a_filed_return_stops_accruing(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db, penalty_per_day_paise=20_000)
        make_filing(
            db,
            company,
            obligation,
            due_date=TODAY - timedelta(days=10),
            status=FilingStatus.LATE_FILED,
        )

        assert get_dashboard(app_client, company_admin)["penalty_exposure_paise"] == 0


class TestUpcomingAndOverdueLists:
    def test_upcoming_carries_what_the_card_renders(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(
            db, title="Monthly GST return", penalty_per_day_paise=20_000
        )
        make_filing(db, company, obligation, due_date=TODAY + timedelta(days=12))

        card = get_dashboard(app_client, company_admin)["upcoming"][0]

        assert card["title"] == "Monthly GST return"
        assert card["due_date"] == (TODAY + timedelta(days=12)).isoformat()
        assert card["days_until_due"] == 12
        assert card["penalty_per_day_paise"] == 20_000
        assert card["status"] == "not_started"

    def test_the_two_lists_are_disjoint(self, app_client, db, company, company_admin):
        obligation = make_obligation(db)
        make_filing(
            db, company, obligation, period_key="a", due_date=TODAY + timedelta(days=3)
        )
        make_filing(
            db, company, obligation, period_key="b", due_date=TODAY - timedelta(days=3)
        )

        body = get_dashboard(app_client, company_admin)

        assert [c["period_key"] for c in body["upcoming"]] == ["a"]
        assert [c["period_key"] for c in body["overdue"]] == ["b"]

    def test_the_horizon_bounds_the_upcoming_list(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(
            db, company, obligation, period_key="a", due_date=TODAY + timedelta(days=5)
        )
        make_filing(
            db, company, obligation, period_key="b", due_date=TODAY + timedelta(days=60)
        )

        assert len(get_dashboard(app_client, company_admin)["upcoming"]) == 1
        assert len(get_dashboard(app_client, company_admin, upcoming_days=90)["upcoming"]) == 2

    def test_the_lists_are_capped_by_limit(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        for n in range(8):
            make_filing(
                db,
                company,
                obligation,
                period_key=f"p{n}",
                due_date=TODAY + timedelta(days=n + 1),
            )

        body = get_dashboard(app_client, company_admin, limit=3)

        assert len(body["upcoming"]) == 3
        # The cap trims the list; it must not distort the counts beside it.
        assert body["score"]["open_filings"] == 8

    def test_upcoming_is_ordered_by_due_date(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        for n in (20, 5, 12):
            make_filing(
                db,
                company,
                obligation,
                period_key=f"p{n}",
                due_date=TODAY + timedelta(days=n),
            )

        dates = [c["due_date"] for c in get_dashboard(app_client, company_admin)["upcoming"]]

        assert dates == sorted(dates)

    def test_a_closed_filing_appears_in_neither_list(
        self, app_client, db, company, company_admin
    ):
        make_filing(
            db,
            company,
            make_obligation(db),
            due_date=TODAY - timedelta(days=3),
            status=FilingStatus.ACKNOWLEDGED,
        )

        body = get_dashboard(app_client, company_admin)

        assert body["upcoming"] == []
        assert body["overdue"] == []

    def test_the_limit_is_bounded_by_the_query_contract(
        self, app_client, company_admin
    ):
        response = app_client.get(
            f"{API}/dashboard", headers=auth(company_admin), params={"limit": 500}
        )

        assert response.status_code == 422


class TestBreakdowns:
    def test_by_regulation_splits_the_counts(
        self, app_client, db, company, company_admin
    ):
        from app.models.enums import Regulation

        gst = make_obligation(db, regulation=Regulation.GST)
        tds = make_obligation(db, regulation=Regulation.INCOME_TAX)
        make_filing(db, company, gst, due_date=TODAY - timedelta(days=2))
        make_filing(
            db, company, gst, period_key="x", due_date=TODAY + timedelta(days=2)
        )
        make_filing(
            db,
            company,
            tds,
            due_date=TODAY - timedelta(days=2),
            status=FilingStatus.SUBMITTED,
        )

        body = get_dashboard(app_client, company_admin)
        rows = {r["regulation"]: r for r in body["by_regulation"]}

        assert rows["gst"] == {
            "regulation": "gst",
            "total": 2,
            "open": 2,
            "overdue": 1,
            "submitted": 0,
        }
        assert rows["income_tax"]["submitted"] == 1
        assert rows["income_tax"]["open"] == 0

    def test_by_status_counts_every_filing(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(db, company, obligation, period_key="a")
        make_filing(db, company, obligation, period_key="b")
        make_filing(
            db, company, obligation, period_key="c", status=FilingStatus.DRAFT
        )

        assert get_dashboard(app_client, company_admin)["by_status"] == {
            "not_started": 2,
            "draft": 1,
        }


class TestSideCounters:
    def test_documents_pending_parse(self, app_client, db, company, company_admin):
        from app.models.enums import ParseStatus

        make_document(db, company)
        make_document(db, company, parse_status=ParseStatus.PROCESSING)
        make_document(db, company, parse_status=ParseStatus.PARSED)

        assert get_dashboard(app_client, company_admin)["documents_pending_parse"] == 2

    def test_open_data_requests_exclude_finished_ones(
        self, app_client, db, company, company_admin
    ):
        make_dsr(db, company)
        make_dsr(db, company, status="completed")
        make_dsr(db, company, status="rejected")

        assert get_dashboard(app_client, company_admin)["open_data_requests"] == 1

    def test_open_breaches_exclude_closed_ones(
        self, app_client, db, company, company_admin
    ):
        make_breach(db, company)
        make_breach(db, company, closed_at=utcnow())

        assert get_dashboard(app_client, company_admin)["open_breaches"] == 1

    def test_the_counters_are_tenant_scoped(
        self, app_client, db, company, other_company, company_admin
    ):
        make_document(db, other_company)
        make_dsr(db, other_company)
        make_breach(db, other_company)

        body = get_dashboard(app_client, company_admin)

        assert body["documents_pending_parse"] == 0
        assert body["open_data_requests"] == 0
        assert body["open_breaches"] == 0


class TestDashboardAccess:
    def test_it_needs_a_token(self, app_client):
        assert app_client.get(f"{API}/dashboard").status_code == 401

    def test_a_read_only_user_may_see_it(
        self, app_client, db, company, company_reader
    ):
        make_filing(db, company, make_obligation(db))

        response = app_client.get(f"{API}/dashboard", headers=auth(company_reader))

        assert response.status_code == 200

    def test_another_tenants_filings_are_not_counted(
        self, app_client, db, company, other_company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(db, other_company, obligation, due_date=TODAY - timedelta(days=30))

        body = get_dashboard(app_client, company_admin)

        assert body["score"]["total_filings"] == 0
        assert body["organization_id"] == company.id

    def test_a_firm_user_acting_for_a_client_sees_the_clients_position(
        self, app_client, db, ca_firm, company, firm_admin, engagement
    ):
        make_filing(db, company, make_obligation(db), due_date=TODAY - timedelta(days=5))

        body = app_client.get(
            f"{API}/dashboard",
            headers=auth(firm_admin, client_org_id=company.id),
            params=PARAMS,
        ).json()

        assert body["organization_id"] == company.id
        assert body["organization_name"] == company.name
        assert body["score"]["overdue_filings"] == 1


class TestFirmDashboard:
    def test_a_company_user_is_refused(self, app_client, company_admin):
        response = app_client.get(f"{API}/dashboard/firm", headers=auth(company_admin))

        assert response.status_code == 403
        assert "CA firm" in response.json()["error"]["message"]

    def test_it_lists_every_client_with_a_score(
        self, app_client, db, ca_firm, company, firm_admin, engagement
    ):
        make_filing(db, company, make_obligation(db), due_date=TODAY - timedelta(days=5))

        body = app_client.get(
            f"{API}/dashboard/firm", headers=auth(firm_admin), params=PARAMS
        ).json()

        assert body["ca_firm_id"] == ca_firm.id
        assert body["client_count"] == 1
        row = body["clients"][0]
        assert row["organization_id"] == company.id
        assert row["overdue_filings"] == 1
        assert row["score"] < 100

    def test_the_totals_are_the_sum_across_clients(
        self, app_client, db, ca_firm, firm_admin
    ):
        obligation = make_obligation(db, penalty_per_day_paise=10_000)
        for name in ("Alpha Ltd", "Beta Ltd"):
            client_org = make_org(db, name=name)
            make_engagement(db, ca_firm, client_org)
            make_filing(
                db, client_org, obligation, due_date=TODAY - timedelta(days=4)
            )

        body = app_client.get(
            f"{API}/dashboard/firm", headers=auth(firm_admin), params=PARAMS
        ).json()

        assert body["total_overdue_filings"] == 2
        assert body["total_open_filings"] == 2
        assert body["total_penalty_exposure_paise"] == 80_000

    def test_the_attention_queue_is_worst_first(
        self, app_client, db, ca_firm, firm_admin
    ):
        obligation = make_obligation(db)
        for name, overdue_count in (("Alpha Ltd", 1), ("Beta Ltd", 4), ("Gamma Ltd", 0)):
            client_org = make_org(db, name=name)
            make_engagement(db, ca_firm, client_org)
            for n in range(overdue_count):
                make_filing(
                    db,
                    client_org,
                    obligation,
                    period_key=f"p{n}",
                    due_date=TODAY - timedelta(days=5),
                )

        body = app_client.get(
            f"{API}/dashboard/firm", headers=auth(firm_admin), params=PARAMS
        ).json()

        # Alphabetical in `clients`, worst-first in `attention_required`. The
        # two orderings answer different questions and must not be conflated.
        assert [c["name"] for c in body["clients"]] == [
            "Alpha Ltd",
            "Beta Ltd",
            "Gamma Ltd",
        ]
        assert [c["name"] for c in body["attention_required"]] == [
            "Beta Ltd",
            "Alpha Ltd",
        ]

    def test_exposure_breaks_a_tie_on_overdue_count(
        self, app_client, db, ca_firm, firm_admin
    ):
        cheap = make_obligation(db, penalty_per_day_paise=1_000)
        dear = make_obligation(db, penalty_per_day_paise=50_000)
        for name, obligation in (("Alpha Ltd", cheap), ("Beta Ltd", dear)):
            client_org = make_org(db, name=name)
            make_engagement(db, ca_firm, client_org)
            make_filing(
                db, client_org, obligation, due_date=TODAY - timedelta(days=5)
            )

        body = app_client.get(
            f"{API}/dashboard/firm", headers=auth(firm_admin), params=PARAMS
        ).json()

        assert [c["name"] for c in body["attention_required"]] == [
            "Beta Ltd",
            "Alpha Ltd",
        ]

    def test_the_next_due_filing_is_reported(
        self, app_client, db, ca_firm, company, firm_admin, engagement
    ):
        obligation = make_obligation(db, filing_type="GSTR-3B")
        make_filing(
            db, company, obligation, period_key="a", due_date=TODAY + timedelta(days=40)
        )
        make_filing(
            db, company, obligation, period_key="b", due_date=TODAY + timedelta(days=6)
        )

        row = app_client.get(
            f"{API}/dashboard/firm", headers=auth(firm_admin), params=PARAMS
        ).json()["clients"][0]

        assert row["next_due_date"] == (TODAY + timedelta(days=6)).isoformat()
        assert row["next_filing_type"] == "GSTR-3B"
        assert row["due_within_7_days"] == 1

    def test_a_firm_with_no_clients_averages_100(
        self, app_client, ca_firm, firm_admin
    ):
        body = app_client.get(
            f"{API}/dashboard/firm", headers=auth(firm_admin), params=PARAMS
        ).json()

        assert body["clients"] == []
        assert body["average_score"] == 100
        assert body["attention_required"] == []

    def test_staff_see_only_the_clients_they_are_assigned(
        self, app_client, db, ca_firm, firm_staff
    ):
        mine = make_org(db, name="Assigned Ltd")
        theirs = make_org(db, name="Unassigned Ltd")
        assigned = make_engagement(db, ca_firm, mine)
        make_engagement(db, ca_firm, theirs)
        make_assignment(db, firm_staff, assigned)

        body = app_client.get(
            f"{API}/dashboard/firm", headers=auth(firm_staff), params=PARAMS
        ).json()

        assert [c["name"] for c in body["clients"]] == ["Assigned Ltd"]
        assert body["client_count"] == 1

    def test_a_manager_sees_the_whole_book(self, app_client, db, ca_firm):
        manager = make_user(db, ca_firm, role=UserRole.COMPLIANCE_MANAGER)
        for name in ("Alpha Ltd", "Beta Ltd"):
            make_engagement(db, ca_firm, make_org(db, name=name))

        body = app_client.get(
            f"{API}/dashboard/firm", headers=auth(manager), params=PARAMS
        ).json()

        assert body["client_count"] == 2

    def test_another_firms_engagements_are_invisible(
        self, app_client, db, ca_firm, firm_admin
    ):
        rival_firm = make_org(db, name="Rival & Co", type=ca_firm.type)
        make_engagement(db, rival_firm, make_org(db, name="Their Client Ltd"))

        body = app_client.get(
            f"{API}/dashboard/firm", headers=auth(firm_admin), params=PARAMS
        ).json()

        assert body["clients"] == []


class TestWorkload:
    def test_it_counts_a_staff_members_assigned_filings(
        self, app_client, db, ca_firm, firm_admin, firm_staff
    ):
        client_org = make_org(db, name="Assigned Ltd")
        make_engagement(db, ca_firm, client_org, assigned_user_id=firm_staff.id)
        obligation = make_obligation(db)
        make_filing(
            db, client_org, obligation, period_key="a", due_date=TODAY - timedelta(days=2)
        )
        make_filing(
            db, client_org, obligation, period_key="b", due_date=TODAY + timedelta(days=3)
        )

        rows = app_client.get(
            f"{API}/dashboard/firm/workload", headers=auth(firm_admin), params=PARAMS
        ).json()
        row = next(r for r in rows if r["user_id"] == firm_staff.id)

        assert row["client_count"] == 1
        assert row["open_filings"] == 2
        assert row["overdue_filings"] == 1
        assert row["due_within_7_days"] == 1

    def test_an_unassigned_member_reports_zeroes(
        self, app_client, db, ca_firm, firm_admin, firm_staff
    ):
        rows = app_client.get(
            f"{API}/dashboard/firm/workload", headers=auth(firm_admin), params=PARAMS
        ).json()
        row = next(r for r in rows if r["user_id"] == firm_staff.id)

        assert row == {
            "user_id": firm_staff.id,
            "full_name": firm_staff.full_name,
            "role": "staff",
            "client_count": 0,
            "open_filings": 0,
            "overdue_filings": 0,
            "due_within_7_days": 0,
        }

    def test_staff_may_not_read_it(self, app_client, ca_firm, firm_staff):
        response = app_client.get(
            f"{API}/dashboard/firm/workload", headers=auth(firm_staff)
        )

        assert response.status_code == 403
        assert "compliance_manager" in response.json()["error"]["message"]

    def test_a_manager_may_read_it(self, app_client, db, ca_firm):
        manager = make_user(db, ca_firm, role=UserRole.COMPLIANCE_MANAGER)

        response = app_client.get(
            f"{API}/dashboard/firm/workload", headers=auth(manager), params=PARAMS
        )

        assert response.status_code == 200

    def test_a_company_user_is_refused_before_the_role_check(
        self, app_client, company_admin
    ):
        response = app_client.get(
            f"{API}/dashboard/firm/workload", headers=auth(company_admin)
        )

        assert response.status_code == 403
        assert "CA firm" in response.json()["error"]["message"]

    def test_a_deactivated_member_is_left_out(
        self, app_client, db, ca_firm, firm_admin, firm_staff
    ):
        firm_staff.is_active = False
        db.flush()

        rows = app_client.get(
            f"{API}/dashboard/firm/workload", headers=auth(firm_admin), params=PARAMS
        ).json()

        assert firm_staff.id not in [r["user_id"] for r in rows]

    def test_only_this_firms_staff_are_listed(
        self, app_client, db, ca_firm, firm_admin, other_admin
    ):
        rows = app_client.get(
            f"{API}/dashboard/firm/workload", headers=auth(firm_admin), params=PARAMS
        ).json()

        assert other_admin.id not in [r["user_id"] for r in rows]
