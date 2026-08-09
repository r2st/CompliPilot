"""Filings: the CRUD, and the workflow that status changes have to go through.

The design decision this file exists to protect is that ``status`` is not a
patchable field. Every move goes through ``POST /filings/{id}/transition``, and
if a PATCH could set it, all of :mod:`app.services.filing_workflow` — the legal
transition table, the per-target role floors, the preparer-is-not-approver
rule — could be skipped with an ordinary update. So there is a test that a
PATCH carrying a status leaves the status alone, and there are tests for each
guard behind the transition endpoint.

Two other properties get their own tests because they are easy to regress and
expensive to get wrong:

* a SUBMITTED after the due date is silently recorded as LATE_FILED, because
  leaving it as SUBMITTED hides a penalty exposure from the report a CA runs
  to find them;
* an already-submitted filing can be neither edited nor voided, since our copy
  diverging from what went to the regulator is the one thing the audit trail
  exists to prevent.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.models.enums import AuditAction, FilingStatus, Frequency, UserRole
from app.models.filing import Deadline, Filing
from tests.conftest import (
    API,
    auth,
    make_deadline,
    make_engagement,
    make_filing,
    make_obligation,
    make_org,
    make_user,
)

TODAY = date(2026, 8, 8)
PAST = date(2026, 7, 1)
FUTURE = date(2026, 12, 31)


def _deadline_for(db, filing: Filing) -> Deadline | None:
    return db.query(Deadline).filter_by(filing_id=filing.id).one_or_none()


class TestFilingListing:
    def test_a_tenant_sees_only_their_own_filings(
        self, app_client, db, company, other_company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(db, company, obligation)
        make_filing(db, other_company, obligation)

        response = app_client.get(f"{API}/filings", headers=auth(company_admin))

        assert response.json()["total"] == 1

    def test_the_summary_carries_the_derived_fields(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db, title="Monthly GST return")
        make_filing(db, company, obligation, due_date=date(2026, 8, 20))

        item = app_client.get(
            f"{API}/filings", headers=auth(company_admin), params={"today": TODAY.isoformat()}
        ).json()["items"][0]

        assert item["title"] == "Monthly GST return"
        assert item["effective_due_date"] == "2026-08-20"
        assert item["days_until_due"] == 12
        assert item["urgency"]
        assert item["is_open"] is True

    def test_an_extension_moves_the_effective_due_date(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(
            db,
            company,
            obligation,
            due_date=date(2026, 8, 20),
            extended_due_date=date(2026, 9, 30),
        )

        item = app_client.get(
            f"{API}/filings", headers=auth(company_admin), params={"today": TODAY.isoformat()}
        ).json()["items"][0]

        assert item["effective_due_date"] == "2026-09-30"

    def test_filtering_by_status(self, app_client, db, company, company_admin):
        obligation = make_obligation(db)
        make_filing(db, company, obligation, period_key="2026-06", status=FilingStatus.DRAFT)
        make_filing(db, company, obligation, period_key="2026-07")

        response = app_client.get(
            f"{API}/filings", headers=auth(company_admin), params={"status": "draft"}
        )

        assert response.json()["total"] == 1

    def test_filtering_by_regulation_and_period(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(db, company, obligation, period_key="2026-06")
        make_filing(db, company, obligation, period_key="2026-07")

        response = app_client.get(
            f"{API}/filings",
            headers=auth(company_admin),
            params={"regulation": "gst", "period_key": "2026-07"},
        )

        assert response.json()["total"] == 1

    def test_filtering_by_the_obligations_frequency(
        self, app_client, db, company, company_admin
    ):
        monthly = make_obligation(db, code="m", frequency=Frequency.MONTHLY)
        annual = make_obligation(db, code="a", frequency=Frequency.ANNUAL, due_day=31, due_month=12)
        make_filing(db, company, monthly, period_key="2026-07")
        make_filing(db, company, annual, period_key="2026")

        response = app_client.get(
            f"{API}/filings", headers=auth(company_admin), params={"frequency": "annual"}
        )

        assert response.json()["total"] == 1

    def test_only_open_excludes_the_finished_ones(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(db, company, obligation, period_key="2026-05", status=FilingStatus.SUBMITTED)
        make_filing(db, company, obligation, period_key="2026-06", status=FilingStatus.ACKNOWLEDGED)
        make_filing(
            db, company, obligation, period_key="2026-07", status=FilingStatus.NOT_APPLICABLE
        )
        make_filing(db, company, obligation, period_key="2026-08", status=FilingStatus.DRAFT)

        response = app_client.get(
            f"{API}/filings", headers=auth(company_admin), params={"only_open": True}
        )

        assert response.json()["total"] == 1

    def test_overdue_only_uses_the_extension_when_there_is_one(
        self, app_client, db, company, company_admin
    ):
        """An extension exists precisely to stop something counting as late."""
        obligation = make_obligation(db)
        make_filing(db, company, obligation, period_key="2026-05", due_date=PAST)
        make_filing(
            db,
            company,
            obligation,
            period_key="2026-06",
            due_date=PAST,
            extended_due_date=FUTURE,
        )

        response = app_client.get(
            f"{API}/filings",
            headers=auth(company_admin),
            params={"overdue_only": True, "today": TODAY.isoformat()},
        )

        assert response.json()["total"] == 1
        assert response.json()["items"][0]["period_key"] == "2026-05"

    def test_due_before_and_due_after_bracket_the_window(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(db, company, obligation, period_key="2026-05", due_date=date(2026, 6, 20))
        make_filing(db, company, obligation, period_key="2026-06", due_date=date(2026, 7, 20))
        make_filing(db, company, obligation, period_key="2026-07", due_date=date(2026, 8, 20))

        response = app_client.get(
            f"{API}/filings",
            headers=auth(company_admin),
            params={"due_after": "2026-07-01", "due_before": "2026-08-01"},
        )

        assert response.json()["total"] == 1

    def test_search_covers_the_acknowledgement_number(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(db, company, obligation, acknowledgement_no="ACK-99887766")

        response = app_client.get(
            f"{API}/filings", headers=auth(company_admin), params={"search": "99887766"}
        )

        assert response.json()["total"] == 1

    def test_soft_deleted_filings_are_not_listed(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation)
        filing.soft_delete()
        db.flush()

        assert app_client.get(f"{API}/filings", headers=auth(company_admin)).json()["total"] == 0

    def test_pagination_reports_the_unpaged_total(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        for month in range(1, 8):
            make_filing(db, company, obligation, period_key=f"2026-0{month}")

        response = app_client.get(
            f"{API}/filings", headers=auth(company_admin), params={"limit": 3}
        )

        assert response.json()["total"] == 7
        assert len(response.json()["items"]) == 3


class TestFilingDetail:
    def test_the_detail_lists_the_transitions_the_client_should_offer(
        self, app_client, db, company, company_admin
    ):
        """So the UI renders the right buttons instead of discovering the
        answer via a 409."""
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, status=FilingStatus.IN_REVIEW)

        response = app_client.get(f"{API}/filings/{filing.id}", headers=auth(company_admin))

        assert set(response.json()["allowed_transitions"]) == {"approved", "draft"}

    def test_an_acknowledged_filing_offers_nothing(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, status=FilingStatus.ACKNOWLEDGED)

        response = app_client.get(f"{API}/filings/{filing.id}", headers=auth(company_admin))

        assert response.json()["allowed_transitions"] == []

    def test_the_detail_carries_the_obligations_penalty_text(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(
            db, penalty_description="₹50 per day, capped at ₹5,000", code="pen"
        )
        filing = make_filing(db, company, obligation)

        response = app_client.get(f"{API}/filings/{filing.id}", headers=auth(company_admin))

        assert response.json()["penalty_description"] == "₹50 per day, capped at ₹5,000"

    def test_another_tenants_filing_is_a_404(
        self, app_client, db, other_company, company_admin
    ):
        obligation = make_obligation(db)
        filing = make_filing(db, other_company, obligation)

        response = app_client.get(f"{API}/filings/{filing.id}", headers=auth(company_admin))

        assert response.status_code == 404


class TestFilingCreation:
    def test_staff_can_create_a_filing_with_an_explicit_due_date(
        self, app_client, db, company, company_staff
    ):
        obligation = make_obligation(db)

        response = app_client.post(
            f"{API}/filings",
            headers=auth(company_staff),
            json={
                "obligation_id": obligation.id,
                "period_key": "2026-07",
                "due_date": "2026-08-20",
            },
        )

        assert response.status_code == 201
        assert response.json()["due_date"] == "2026-08-20"
        assert response.json()["status"] == "not_started"
        assert response.json()["prepared_by_id"] == company_staff.id

    def test_the_due_date_is_derived_from_the_obligation_when_omitted(
        self, app_client, db, company, company_admin
    ):
        """The statutory date is the obligation's property; a client that had
        to compute it would be a second deadline engine."""
        obligation = make_obligation(db, frequency=Frequency.MONTHLY, due_day=20)

        response = app_client.post(
            f"{API}/filings",
            headers=auth(company_admin),
            json={
                "obligation_id": obligation.id,
                "period_key": "2026-07",
                "period_start": "2026-07-01",
            },
        )

        assert response.status_code == 201
        assert response.json()["due_date"] == "2026-08-20"

    def test_neither_a_due_date_nor_a_period_start_is_a_validation_error(
        self, app_client, db, company_admin
    ):
        obligation = make_obligation(db)

        response = app_client.post(
            f"{API}/filings",
            headers=auth(company_admin),
            json={"obligation_id": obligation.id, "period_key": "2026-07"},
        )

        assert response.status_code == 422

    def test_an_inverted_period_is_refused(self, app_client, db, company_admin):
        obligation = make_obligation(db)

        response = app_client.post(
            f"{API}/filings",
            headers=auth(company_admin),
            json={
                "obligation_id": obligation.id,
                "period_key": "2026-07",
                "due_date": "2026-08-20",
                "period_start": "2026-07-31",
                "period_end": "2026-07-01",
            },
        )

        assert response.status_code == 422

    def test_a_duplicate_period_is_a_conflict_naming_the_existing_filing(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        existing = make_filing(db, company, obligation, period_key="2026-07")

        response = app_client.post(
            f"{API}/filings",
            headers=auth(company_admin),
            json={
                "obligation_id": obligation.id,
                "period_key": "2026-07",
                "due_date": "2026-08-20",
            },
        )

        assert response.status_code == 409
        assert response.json()["error"]["details"]["filing_id"] == existing.id

    def test_an_unknown_obligation_is_a_404(self, app_client, company_admin):
        response = app_client.post(
            f"{API}/filings",
            headers=auth(company_admin),
            json={"obligation_id": 999_999, "period_key": "2026-07", "due_date": "2026-08-20"},
        )

        assert response.status_code == 404

    def test_another_tenants_obligation_is_a_404(
        self, app_client, db, other_company, company_admin
    ):
        obligation = make_obligation(db, organization_id=other_company.id)

        response = app_client.post(
            f"{API}/filings",
            headers=auth(company_admin),
            json={
                "obligation_id": obligation.id,
                "period_key": "2026-07",
                "due_date": "2026-08-20",
            },
        )

        assert response.status_code == 404

    def test_a_read_only_user_cannot_create(self, app_client, db, company_reader):
        obligation = make_obligation(db)

        response = app_client.post(
            f"{API}/filings",
            headers=auth(company_reader),
            json={
                "obligation_id": obligation.id,
                "period_key": "2026-07",
                "due_date": "2026-08-20",
            },
        )

        assert response.status_code == 403

    def test_a_manual_filing_gets_a_deadline_row_like_a_generated_one(
        self, app_client, db, company, company_admin
    ):
        """Or there is a class of filing that silently never reminds anybody."""
        obligation = make_obligation(db)

        response = app_client.post(
            f"{API}/filings",
            headers=auth(company_admin),
            json={
                "obligation_id": obligation.id,
                "period_key": "2026-07",
                "due_date": "2026-08-20",
            },
        )

        deadline = db.query(Deadline).filter_by(filing_id=response.json()["id"]).one()
        assert deadline.due_date == date(2026, 8, 20)
        assert deadline.is_satisfied is False


class TestFilingEditing:
    def test_editing_the_payload(self, app_client, db, company, company_staff):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation)

        response = app_client.patch(
            f"{API}/filings/{filing.id}",
            headers=auth(company_staff),
            json={"data_json": {"total_turnover": 450000}, "tax_payable_paise": 81000_00},
        )

        assert response.status_code == 200
        assert response.json()["data_json"] == {"total_turnover": 450000}
        assert response.json()["tax_payable_paise"] == 81000_00

    def test_a_status_in_the_patch_body_is_ignored(
        self, app_client, db, company, company_staff
    ):
        """The whole reason the transition endpoint exists: if status were
        patchable, every workflow guard could be skipped with an update."""
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation)

        response = app_client.patch(
            f"{API}/filings/{filing.id}",
            headers=auth(company_staff),
            json={"status": "acknowledged", "notes": "trying it on"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "not_started"

    def test_a_submitted_filing_cannot_be_edited(
        self, app_client, db, company, company_staff
    ):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, status=FilingStatus.SUBMITTED)

        response = app_client.patch(
            f"{API}/filings/{filing.id}",
            headers=auth(company_staff),
            json={"notes": "changing the numbers after the fact"},
        )

        assert response.status_code == 409
        assert "revised return" in response.json()["error"]["message"]

    def test_an_acknowledged_filing_cannot_be_edited(
        self, app_client, db, company, company_staff
    ):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, status=FilingStatus.ACKNOWLEDGED)

        response = app_client.patch(
            f"{API}/filings/{filing.id}", headers=auth(company_staff), json={"notes": "no"}
        )

        assert response.status_code == 409

    def test_an_extension_earlier_than_the_due_date_is_refused(
        self, app_client, db, company, company_staff
    ):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, due_date=date(2026, 8, 20))

        response = app_client.patch(
            f"{API}/filings/{filing.id}",
            headers=auth(company_staff),
            json={"extended_due_date": "2026-08-01"},
        )

        assert response.status_code == 422

    def test_an_extension_moves_the_deadline_row_with_it(
        self, app_client, db, company, company_staff
    ):
        """Or reminders keep firing against a date the regulator has moved."""
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, due_date=date(2026, 8, 20))
        make_deadline(db, filing)

        app_client.patch(
            f"{API}/filings/{filing.id}",
            headers=auth(company_staff),
            json={"extended_due_date": "2026-09-30"},
        )

        assert _deadline_for(db, filing).due_date == date(2026, 9, 30)

    def test_an_unknown_template_is_a_404(self, app_client, db, company, company_staff):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation)

        response = app_client.patch(
            f"{API}/filings/{filing.id}",
            headers=auth(company_staff),
            json={"template_id": 999_999},
        )

        assert response.status_code == 404

    def test_a_read_only_user_cannot_edit(self, app_client, db, company, company_reader):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation)

        response = app_client.patch(
            f"{API}/filings/{filing.id}", headers=auth(company_reader), json={"notes": "no"}
        )

        assert response.status_code == 403


class TestTransitions:
    def test_the_ordinary_path_from_not_started_to_acknowledged(
        self, app_client, db, company, company_admin, company_staff
    ):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, due_date=FUTURE)
        make_deadline(db, filing)
        staff = auth(company_staff)
        admin = auth(company_admin)
        url = f"{API}/filings/{filing.id}/transition"
        when = {"today": TODAY.isoformat()}

        for headers, target in (
            (staff, "draft"),
            (staff, "in_review"),
            (admin, "approved"),
            (admin, "submitted"),
        ):
            step = app_client.post(url, headers=headers, json={"status": target}, params=when)
            assert step.status_code == 200, (target, step.json())

        final = app_client.post(
            url,
            headers=admin,
            json={"status": "acknowledged", "acknowledgement_no": "ACK-123"},
            params=when,
        )

        assert final.json()["filing"]["status"] == "acknowledged"
        assert final.json()["filing"]["acknowledgement_no"] == "ACK-123"

    def test_an_illegal_move_names_the_legal_ones(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, status=FilingStatus.NOT_STARTED)

        response = app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "submitted"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "invalid_transition"
        assert set(response.json()["error"]["details"]["allowed"]) == {
            "draft",
            "not_applicable",
        }

    def test_moving_to_the_status_it_already_has_is_refused(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, status=FilingStatus.DRAFT)

        response = app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "draft"},
        )

        assert response.status_code == 409
        assert "already" in response.json()["error"]["message"]

    def test_an_acknowledged_filing_is_terminal(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, status=FilingStatus.ACKNOWLEDGED)

        response = app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "draft"},
        )

        assert response.status_code == 409

    def test_staff_cannot_approve(self, app_client, db, company, company_staff):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, status=FilingStatus.IN_REVIEW)

        response = app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_staff),
            json={"status": "approved"},
        )

        assert response.status_code == 403
        assert response.json()["error"]["details"]["required_role"] == "compliance_manager"

    def test_staff_cannot_mark_something_not_applicable(
        self, app_client, db, company, company_staff
    ):
        """It is a judgement with a penalty attached if it is wrong."""
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation)

        response = app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_staff),
            json={"status": "not_applicable"},
        )

        assert response.status_code == 403

    def test_the_preparer_may_not_approve_their_own_work(
        self, app_client, db, company, company_admin
    ):
        """Section 4.2 requires human review; a review of your own work is not
        a review."""
        make_user(db, company, role=UserRole.COMPLIANCE_MANAGER)  # a second approver exists
        obligation = make_obligation(db)
        filing = make_filing(
            db,
            company,
            obligation,
            status=FilingStatus.IN_REVIEW,
            prepared_by_id=company_admin.id,
        )

        response = app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "approved"},
        )

        assert response.status_code == 403
        assert response.json()["error"]["details"]["prepared_by_id"] == company_admin.id

    def test_a_one_person_practice_may_approve_their_own(
        self, app_client, db, company, company_admin
    ):
        """The alternative is that a sole practitioner can never file."""
        obligation = make_obligation(db)
        filing = make_filing(
            db,
            company,
            obligation,
            status=FilingStatus.IN_REVIEW,
            prepared_by_id=company_admin.id,
        )

        response = app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "approved"},
        )

        assert response.status_code == 200

    def test_a_colleague_may_approve(self, app_client, db, company, company_admin):
        preparer = make_user(db, company, role=UserRole.STAFF)
        obligation = make_obligation(db)
        filing = make_filing(
            db, company, obligation, status=FilingStatus.IN_REVIEW, prepared_by_id=preparer.id
        )

        response = app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "approved"},
        )

        assert response.status_code == 200
        assert response.json()["filing"]["reviewed_by_id"] == company_admin.id

    def test_submitting_after_the_due_date_is_recorded_as_late_filed(
        self, app_client, db, company, company_admin
    ):
        """Leaving it as SUBMITTED hides a penalty exposure from the very
        report a CA runs to find them."""
        obligation = make_obligation(db)
        filing = make_filing(
            db, company, obligation, due_date=PAST, status=FilingStatus.APPROVED
        )

        response = app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "submitted"},
            params={"today": TODAY.isoformat()},
        )

        assert response.json()["filing"]["status"] == "late_filed"
        assert response.json()["recorded_as_late"] is True

    def test_an_extension_keeps_a_submission_on_time(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        filing = make_filing(
            db,
            company,
            obligation,
            due_date=PAST,
            extended_due_date=FUTURE,
            status=FilingStatus.APPROVED,
        )

        response = app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "submitted"},
            params={"today": TODAY.isoformat()},
        )

        assert response.json()["filing"]["status"] == "submitted"
        assert response.json()["recorded_as_late"] is False

    def test_acknowledging_without_a_number_is_refused(
        self, app_client, db, company, company_admin
    ):
        """The terminal state must not be reachable with nothing to show at an
        assessment."""
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, status=FilingStatus.SUBMITTED)

        response = app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "acknowledged"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["details"]["field"] == "acknowledgement_no"

    def test_a_rejection_needs_a_reason(self, app_client, db, company, company_admin):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, status=FilingStatus.SUBMITTED)

        response = app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "rejected"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["details"]["field"] == "rejection_reason"

    def test_sending_an_approved_filing_back_clears_the_review(
        self, app_client, db, company, company_admin
    ):
        """Or a stale ``reviewed_by`` makes an unreviewed draft look reviewed."""
        preparer = make_user(db, company, role=UserRole.STAFF)
        obligation = make_obligation(db)
        filing = make_filing(
            db, company, obligation, status=FilingStatus.IN_REVIEW, prepared_by_id=preparer.id
        )
        url = f"{API}/filings/{filing.id}/transition"
        app_client.post(url, headers=auth(company_admin), json={"status": "approved"})

        response = app_client.post(
            url, headers=auth(company_admin), json={"status": "draft"}
        )

        assert response.json()["filing"]["reviewed_by_id"] is None
        assert response.json()["filing"]["reviewed_at"] is None

    def test_the_preparer_of_record_is_whoever_sent_it_for_review(
        self, app_client, db, company, company_staff
    ):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, status=FilingStatus.DRAFT)

        response = app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_staff),
            json={"status": "in_review"},
        )

        assert response.json()["filing"]["prepared_by_id"] == company_staff.id

    def test_submitting_satisfies_the_deadline(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        filing = make_filing(
            db, company, obligation, due_date=FUTURE, status=FilingStatus.APPROVED
        )
        make_deadline(db, filing)

        app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "submitted"},
            params={"today": TODAY.isoformat()},
        )

        deadline = _deadline_for(db, filing)
        assert deadline.is_satisfied is True
        assert deadline.satisfied_at is not None

    def test_a_portal_rejection_reopens_the_deadline_and_clears_the_reminders(
        self, app_client, db, company, company_admin
    ):
        """Otherwise a filing reopened five days out never gets chased again,
        because the 30-, 15- and 7-day reminders are all recorded as sent."""
        obligation = make_obligation(db)
        filing = make_filing(
            db, company, obligation, due_date=FUTURE, status=FilingStatus.SUBMITTED
        )
        deadline = make_deadline(
            db,
            filing,
            is_satisfied=True,
            reminders_sent_json=[30, 15, 7],
            escalation_level=2,
        )

        app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "rejected", "rejection_reason": "Mismatch in ITC claimed"},
        )

        db.refresh(deadline)
        assert deadline.is_satisfied is False
        assert deadline.reminders_sent_json == []
        assert deadline.escalation_level == 0

    def test_the_transition_is_audited_with_the_action_matching_the_target(
        self, app_client, db, company, company_admin
    ):
        from app.models.audit import AuditTrail

        preparer = make_user(db, company, role=UserRole.STAFF)
        obligation = make_obligation(db)
        filing = make_filing(
            db, company, obligation, status=FilingStatus.IN_REVIEW, prepared_by_id=preparer.id
        )

        app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "approved"},
        )

        entry = db.query(AuditTrail).filter_by(entity_type="filing").one()
        assert entry.action == AuditAction.APPROVE
        assert entry.before_json["status"] == "in_review"
        assert entry.after_json["status"] == "approved"

    def test_a_late_submission_says_so_in_the_audit_summary(
        self, app_client, db, company, company_admin
    ):
        from app.models.audit import AuditTrail

        obligation = make_obligation(db)
        filing = make_filing(
            db, company, obligation, due_date=PAST, status=FilingStatus.APPROVED
        )

        app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "submitted"},
            params={"today": TODAY.isoformat()},
        )

        entry = db.query(AuditTrail).filter_by(entity_type="filing").one()
        assert "recorded as late" in entry.summary


class TestEventFilings:
    def test_recording_an_event_creates_the_filing_with_the_statutory_offset(
        self, app_client, db, company, company_admin
    ):
        """FC-GPR is due 30 days after an allotment; the allotment is the input."""
        obligation = make_obligation(
            db,
            code="fema.fcgpr",
            frequency=Frequency.EVENT_BASED,
            due_day=None,
            offset_days=30,
            filing_type="FC-GPR",
        )

        response = app_client.post(
            f"{API}/filings/events",
            headers=auth(company_admin),
            json={"obligation_id": obligation.id, "event_date": "2026-08-01"},
        )

        assert response.status_code == 201
        assert response.json()["due_date"] == "2026-08-31"
        assert response.json()["prepared_by_id"] == company_admin.id

    def test_the_same_event_twice_is_a_conflict(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(
            db, frequency=Frequency.EVENT_BASED, due_day=None, offset_days=30
        )
        body = {"obligation_id": obligation.id, "event_date": "2026-08-01"}
        app_client.post(f"{API}/filings/events", headers=auth(company_admin), json=body)

        response = app_client.post(
            f"{API}/filings/events", headers=auth(company_admin), json=body
        )

        assert response.status_code == 409

    def test_a_recurring_obligation_is_refused_with_its_frequency(
        self, app_client, db, company_admin
    ):
        """Those come from the calendar sweep, not from an event."""
        obligation = make_obligation(db, frequency=Frequency.MONTHLY, due_day=20)

        response = app_client.post(
            f"{API}/filings/events",
            headers=auth(company_admin),
            json={"obligation_id": obligation.id, "event_date": "2026-08-01"},
        )

        assert response.status_code == 422
        assert response.json()["error"]["details"]["frequency"] == "monthly"

    def test_a_one_time_obligation_is_accepted(self, app_client, db, company_admin):
        obligation = make_obligation(
            db, frequency=Frequency.ONE_TIME, due_day=None, offset_days=60
        )

        response = app_client.post(
            f"{API}/filings/events",
            headers=auth(company_admin),
            json={"obligation_id": obligation.id, "event_date": "2026-08-01"},
        )

        assert response.status_code == 201

    def test_a_read_only_user_cannot_record_an_event(
        self, app_client, db, company_reader
    ):
        obligation = make_obligation(
            db, frequency=Frequency.EVENT_BASED, due_day=None, offset_days=30
        )

        response = app_client.post(
            f"{API}/filings/events",
            headers=auth(company_reader),
            json={"obligation_id": obligation.id, "event_date": "2026-08-01"},
        )

        assert response.status_code == 403


class TestVoiding:
    def test_a_manager_voids_a_filing(self, app_client, db, company, company_admin):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation)
        deadline = make_deadline(db, filing)

        response = app_client.delete(
            f"{API}/filings/{filing.id}", headers=auth(company_admin)
        )

        assert response.status_code == 200
        db.refresh(filing)
        db.refresh(deadline)
        assert filing.deleted_at is not None
        assert deadline.deleted_at is not None

    def test_a_submitted_filing_cannot_be_voided(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation, status=FilingStatus.SUBMITTED)

        response = app_client.delete(
            f"{API}/filings/{filing.id}", headers=auth(company_admin)
        )

        assert response.status_code == 409

    def test_staff_cannot_void(self, app_client, db, company, company_staff):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation)

        response = app_client.delete(
            f"{API}/filings/{filing.id}", headers=auth(company_staff)
        )

        assert response.status_code == 403

    def test_voiding_twice_is_a_404(self, app_client, db, company, company_admin):
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation)
        app_client.delete(f"{API}/filings/{filing.id}", headers=auth(company_admin))

        response = app_client.delete(
            f"{API}/filings/{filing.id}", headers=auth(company_admin)
        )

        assert response.status_code == 404


class TestDelegatedFilingAccess:
    def test_a_firm_acting_for_a_client_works_on_the_clients_filings(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        make_engagement(db, ca_firm, company)
        obligation = make_obligation(db)
        make_filing(db, company, obligation)

        response = app_client.get(
            f"{API}/filings", headers=auth(firm_admin, client_org_id=company.id)
        )

        assert response.json()["total"] == 1

    def test_a_firm_without_a_client_context_sees_its_own_filings_only(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        make_engagement(db, ca_firm, company)
        obligation = make_obligation(db)
        make_filing(db, company, obligation)

        response = app_client.get(f"{API}/filings", headers=auth(firm_admin))

        assert response.json()["total"] == 0

    def test_a_read_only_grant_blocks_writes_on_that_client(
        self, app_client, db, ca_firm, company
    ):
        from tests.conftest import make_assignment

        staff = make_user(db, ca_firm, role=UserRole.STAFF)
        engagement = make_engagement(db, ca_firm, company)
        make_assignment(db, staff, engagement, granted_role=UserRole.READ_ONLY)
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation)

        response = app_client.patch(
            f"{API}/filings/{filing.id}",
            headers=auth(staff, client_org_id=company.id),
            json={"notes": "no"},
        )

        assert response.status_code == 403

    def test_the_filing_of_a_client_the_firm_does_not_act_for_is_invisible(
        self, app_client, db, ca_firm, firm_admin, company, other_company
    ):
        make_engagement(db, ca_firm, company)
        obligation = make_obligation(db)
        filing = make_filing(db, other_company, obligation)

        response = app_client.get(
            f"{API}/filings/{filing.id}", headers=auth(firm_admin, client_org_id=company.id)
        )

        assert response.status_code == 404


class TestUrgencyBands:
    def test_an_overdue_filing_reports_negative_days(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(db, company, obligation, due_date=TODAY - timedelta(days=5))

        item = app_client.get(
            f"{API}/filings", headers=auth(company_admin), params={"today": TODAY.isoformat()}
        ).json()["items"][0]

        assert item["days_until_due"] == -5
        assert item["urgency"] == "overdue"

    def test_a_filing_due_today_is_not_overdue(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        make_filing(db, company, obligation, due_date=TODAY)

        item = app_client.get(
            f"{API}/filings", headers=auth(company_admin), params={"today": TODAY.isoformat()}
        ).json()["items"][0]

        assert item["days_until_due"] == 0
        assert item["urgency"] != "overdue"


class TestCrossTenantWrites:
    def test_a_filing_cannot_be_transitioned_across_tenants(
        self, app_client, db, other_company, company_admin
    ):
        obligation = make_obligation(db)
        filing = make_filing(db, other_company, obligation)

        response = app_client.post(
            f"{API}/filings/{filing.id}/transition",
            headers=auth(company_admin),
            json={"status": "draft"},
        )

        assert response.status_code == 404

    def test_a_third_organizations_admin_cannot_void(self, app_client, db, company):
        third = make_org(db, name="Third Party Ltd")
        third_admin = make_user(db, third, role=UserRole.ADMIN)
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation)

        response = app_client.delete(
            f"{API}/filings/{filing.id}", headers=auth(third_admin)
        )

        assert response.status_code == 404
