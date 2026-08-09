"""The organization profile and the CA firm's client book.

Two things are load-bearing here and neither is obvious from the route
signatures.

The first is *which* organization a request acts on. Profile routes read
``ctx.org_id`` — the organization being acted for, so a CA editing a client's
turnover edits the client's profile. Engagement routes read ``home_org_id`` —
the firm — because an engagement belongs to the firm and a client list that
emptied the moment someone switched context would be useless exactly when it is
needed. Every test below that switches context first is testing that
distinction.

The second is that a row belonging to another tenant answers 404 and never 403.
403 confirms the id exists, which is enough to walk the id space and count a
competitor's clients.
"""
from __future__ import annotations

from datetime import date

from app.core.crypto import fingerprint
from app.models.enums import AuditAction, EngagementStatus, OrgType, UserRole
from app.models.organization import Client, Organization
from tests.conftest import (
    API,
    CRORE,
    auth,
    make_assignment,
    make_engagement,
    make_filing,
    make_obligation,
    make_org,
    make_user,
)


class TestProfileRead:
    def test_a_company_user_reads_their_own_profile(self, app_client, company, company_admin):
        response = app_client.get(f"{API}/organizations/me", headers=auth(company_admin))

        assert response.status_code == 200
        assert response.json()["id"] == company.id
        assert response.json()["name"] == company.name

    def test_a_firm_in_a_clients_context_reads_the_clients_profile(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        """One profile screen serves both cases without the frontend knowing
        which it is in."""
        make_engagement(db, ca_firm, company)

        response = app_client.get(
            f"{API}/organizations/me", headers=auth(firm_admin, client_org_id=company.id)
        )

        assert response.json()["id"] == company.id

    def test_the_identifiers_come_back_decrypted(self, app_client, db, company, company_admin):
        from app.core.crypto import encrypt

        company.gstin = encrypt("29ABCDE1234F1Z5")
        company.gstin_fingerprint = fingerprint("29ABCDE1234F1Z5")
        db.flush()

        response = app_client.get(f"{API}/organizations/me", headers=auth(company_admin))

        assert response.json()["gstin"] == "29ABCDE1234F1Z5"

    def test_a_read_only_user_may_read_the_profile(self, app_client, company_reader):
        assert (
            app_client.get(f"{API}/organizations/me", headers=auth(company_reader)).status_code
            == 200
        )


class TestProfileUpdate:
    def test_a_manager_can_edit_the_profile(self, app_client, db, company):
        manager = make_user(db, company, role=UserRole.COMPLIANCE_MANAGER)

        response = app_client.patch(
            f"{API}/organizations/me",
            headers=auth(manager),
            json={"annual_turnover_paise": 8 * CRORE, "industry": "Textiles"},
        )

        assert response.status_code == 200
        assert response.json()["annual_turnover_paise"] == 8 * CRORE
        assert response.json()["industry"] == "Textiles"

    def test_staff_cannot(self, app_client, company_staff):
        """These fields drive applicability — ₹4cr to ₹6cr adds a tax audit."""
        response = app_client.patch(
            f"{API}/organizations/me",
            headers=auth(company_staff),
            json={"annual_turnover_paise": 8 * CRORE},
        )

        assert response.status_code == 403
        assert response.json()["error"]["details"]["required_role"] == "compliance_manager"

    def test_an_explicit_null_leaves_the_field_alone_rather_than_clearing_it(
        self, app_client, db, company, company_admin
    ):
        """The PATCH schema documents None as "leave alone"; clearing an
        identifier frees a uniqueness slot and deserves its own endpoint."""
        app_client.patch(
            f"{API}/organizations/me",
            headers=auth(company_admin),
            json={"industry": None},
        )

        db.refresh(company)
        response = app_client.get(f"{API}/organizations/me", headers=auth(company_admin))
        assert response.json()["state"] == "Karnataka"

    def test_setting_a_gstin_encrypts_it_and_stores_a_fingerprint(
        self, app_client, db, company, company_admin
    ):
        app_client.patch(
            f"{API}/organizations/me",
            headers=auth(company_admin),
            json={"gstin": "29ABCDE1234F1Z5"},
        )

        db.refresh(company)
        assert company.gstin != "29ABCDE1234F1Z5"  # ciphertext, not plaintext
        assert company.gstin_fingerprint == fingerprint("29ABCDE1234F1Z5")

    def test_a_gstin_already_registered_elsewhere_is_a_named_conflict(
        self, app_client, db, company_admin, other_company
    ):
        """The index would also catch it, but with no field name to show."""
        other_company.gstin_fingerprint = fingerprint("29ABCDE1234F1Z5")
        db.flush()

        response = app_client.patch(
            f"{API}/organizations/me",
            headers=auth(company_admin),
            json={"gstin": "29ABCDE1234F1Z5"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["details"]["field"] == "gstin"

    def test_a_soft_deleted_organization_does_not_block_the_gstin(
        self, app_client, db, company_admin, other_company
    ):
        other_company.gstin_fingerprint = fingerprint("29ABCDE1234F1Z5")
        other_company.soft_delete()
        db.flush()

        response = app_client.patch(
            f"{API}/organizations/me",
            headers=auth(company_admin),
            json={"gstin": "29ABCDE1234F1Z5"},
        )

        assert response.status_code == 200

    def test_only_the_changed_fields_reach_the_audit_entry(
        self, app_client, db, company_admin
    ):
        """An entry repeating forty unchanged columns is one nobody reads."""
        from app.models.audit import AuditTrail

        app_client.patch(
            f"{API}/organizations/me",
            headers=auth(company_admin),
            json={"industry": "Textiles", "state": "Karnataka"},
        )

        entry = db.query(AuditTrail).filter_by(action=AuditAction.UPDATE).one()
        assert set(entry.after_json) == {"industry"}  # state was already Karnataka

    def test_a_no_op_patch_writes_no_audit_entry(self, app_client, db, company_admin):
        from app.models.audit import AuditTrail

        response = app_client.patch(
            f"{API}/organizations/me",
            headers=auth(company_admin),
            json={"state": "Karnataka"},
        )

        assert response.status_code == 200
        assert db.query(AuditTrail).count() == 0

    def test_the_plaintext_identifier_never_appears_in_the_audit_payload(
        self, app_client, db, company_admin
    ):
        from app.models.audit import AuditTrail

        app_client.patch(
            f"{API}/organizations/me",
            headers=auth(company_admin),
            json={"gstin": "29ABCDE1234F1Z5", "industry": "Textiles"},
        )

        payloads = [
            (e.before_json or {}, e.after_json or {}) for e in db.query(AuditTrail).all()
        ]
        assert "29ABCDE1234F1Z5" not in str(payloads)

    def test_a_firm_editing_a_client_edits_the_clients_row(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        make_engagement(db, ca_firm, company)

        app_client.patch(
            f"{API}/organizations/me",
            headers=auth(firm_admin, client_org_id=company.id),
            json={"industry": "Textiles"},
        )

        db.refresh(company)
        db.refresh(ca_firm)
        assert company.industry == "Textiles"
        assert ca_firm.industry is None


class TestApplicabilityAndCoverage:
    def test_a_sync_evaluates_the_catalogue_for_this_organization(
        self, app_client, db, seeded, company, company_admin
    ):
        response = app_client.post(
            f"{API}/organizations/me/applicability/sync", headers=auth(company_admin)
        )

        assert response.status_code == 200
        assert response.json()["evaluated"] > 0

    def test_a_second_sync_creates_nothing_new(
        self, app_client, db, seeded, company_admin
    ):
        app_client.post(
            f"{API}/organizations/me/applicability/sync", headers=auth(company_admin)
        )

        second = app_client.post(
            f"{API}/organizations/me/applicability/sync", headers=auth(company_admin)
        )

        assert second.json()["created"] == 0

    def test_staff_cannot_trigger_a_sync(self, app_client, seeded, company_staff):
        response = app_client.post(
            f"{API}/organizations/me/applicability/sync", headers=auth(company_staff)
        )

        assert response.status_code == 403

    def test_coverage_reports_per_regulation_totals(
        self, app_client, db, seeded, company_admin
    ):
        app_client.post(
            f"{API}/organizations/me/applicability/sync", headers=auth(company_admin)
        )

        response = app_client.get(
            f"{API}/organizations/me/coverage", headers=auth(company_admin)
        )

        body = response.json()
        assert body["total_applicable"] > 0
        assert body["total_evaluated"] >= body["total_applicable"]
        assert body["by_regulation"]

    def test_coverage_is_empty_before_a_sync(self, app_client, seeded, company_admin):
        response = app_client.get(
            f"{API}/organizations/me/coverage", headers=auth(company_admin)
        )

        assert response.json()["total_evaluated"] == 0


class TestDeactivation:
    def test_an_admin_deactivates_the_organization(
        self, app_client, db, company, company_admin
    ):
        response = app_client.delete(f"{API}/organizations/me", headers=auth(company_admin))

        assert response.status_code == 200
        db.refresh(company)
        assert company.is_active is False
        assert company.deleted_at is not None

    def test_deactivation_locks_out_the_admin_who_did_it(
        self, app_client, company_admin
    ):
        """``get_current_organization`` refuses an inactive org on the next
        request, including this user's own."""
        app_client.delete(f"{API}/organizations/me", headers=auth(company_admin))

        assert (
            app_client.get(f"{API}/organizations/me", headers=auth(company_admin)).status_code
            == 403
        )

    def test_a_manager_cannot_deactivate(self, app_client, db, company):
        manager = make_user(db, company, role=UserRole.COMPLIANCE_MANAGER)

        response = app_client.delete(f"{API}/organizations/me", headers=auth(manager))

        assert response.status_code == 403


class TestClientListing:
    def test_a_firm_lists_its_engagements(
        self, app_client, db, ca_firm, firm_admin, company, other_company
    ):
        make_engagement(db, ca_firm, company)
        make_engagement(db, ca_firm, other_company)

        response = app_client.get(f"{API}/clients", headers=auth(firm_admin))

        assert response.status_code == 200
        assert response.json()["total"] == 2
        assert {i["client_organization"]["name"] for i in response.json()["items"]} == {
            company.name,
            other_company.name,
        }

    def test_a_company_user_is_refused_rather_than_shown_an_empty_list(
        self, app_client, company_admin
    ):
        """An empty list would suggest the feature is available to them."""
        response = app_client.get(f"{API}/clients", headers=auth(company_admin))

        assert response.status_code == 403

    def test_staff_see_only_their_assignments(
        self, app_client, db, ca_firm, firm_staff, company, other_company
    ):
        assigned = make_engagement(db, ca_firm, company)
        make_engagement(db, ca_firm, other_company)
        make_assignment(db, firm_staff, assigned)

        response = app_client.get(f"{API}/clients", headers=auth(firm_staff))

        assert response.json()["total"] == 1
        assert response.json()["items"][0]["client_org_id"] == company.id

    def test_the_list_stays_the_firms_after_switching_into_a_client(
        self, app_client, db, ca_firm, firm_admin, company, other_company
    ):
        """Reading ``org_id`` here would empty the list exactly when someone
        wants to switch again."""
        make_engagement(db, ca_firm, company)
        make_engagement(db, ca_firm, other_company)

        response = app_client.get(
            f"{API}/clients", headers=auth(firm_admin, client_org_id=company.id)
        )

        assert response.json()["total"] == 2

    def test_a_soft_deleted_engagement_is_not_listed(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        engagement = make_engagement(db, ca_firm, company)
        engagement.soft_delete()
        db.flush()

        assert app_client.get(f"{API}/clients", headers=auth(firm_admin)).json()["total"] == 0

    def test_filtering_by_status(
        self, app_client, db, ca_firm, firm_admin, company, other_company
    ):
        make_engagement(db, ca_firm, company)
        paused = make_engagement(db, ca_firm, other_company)
        paused.status = str(EngagementStatus.PAUSED)
        db.flush()

        response = app_client.get(
            f"{API}/clients", headers=auth(firm_admin), params={"status": "paused"}
        )

        assert response.json()["total"] == 1
        assert response.json()["items"][0]["client_org_id"] == other_company.id

    def test_searching_by_client_name(
        self, app_client, db, ca_firm, firm_admin, company, other_company
    ):
        make_engagement(db, ca_firm, company)
        make_engagement(db, ca_firm, other_company)

        response = app_client.get(
            f"{API}/clients", headers=auth(firm_admin), params={"search": "rival"}
        )

        assert response.json()["total"] == 1

    def test_the_counts_come_from_one_query_and_split_open_from_overdue(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        make_engagement(db, ca_firm, company)
        obligation = make_obligation(db)
        make_filing(db, company, obligation, period_key="2026-06", due_date=date(2020, 1, 1))
        make_filing(db, company, obligation, period_key="2026-07", due_date=date(2099, 1, 1))
        db.flush()

        item = app_client.get(f"{API}/clients", headers=auth(firm_admin)).json()["items"][0]

        assert item["open_filings"] == 2
        assert item["overdue_filings"] == 1

    def test_the_counts_can_be_skipped(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        make_engagement(db, ca_firm, company)

        response = app_client.get(
            f"{API}/clients", headers=auth(firm_admin), params={"with_counts": False}
        )

        assert response.json()["items"][0]["open_filings"] is None

    def test_pagination_reports_the_unpaged_total(
        self, app_client, db, ca_firm, firm_admin
    ):
        for n in range(5):
            make_engagement(db, ca_firm, make_org(db, name=f"Client {n} Pvt Ltd"))

        response = app_client.get(
            f"{API}/clients", headers=auth(firm_admin), params={"limit": 2, "offset": 2}
        )

        assert response.json()["total"] == 5
        assert len(response.json()["items"]) == 2


class TestClientCreation:
    def test_engaging_a_new_organization_creates_it(self, app_client, db, ca_firm, firm_admin):
        response = app_client.post(
            f"{API}/clients",
            headers=auth(firm_admin),
            json={
                "organization": {
                    "name": "Sunrise Exports LLP",
                    "entity_type": "llp",
                    "state": "Gujarat",
                    "gstin": "24ABCDE1234F1Z5",
                },
                "engagement_type": "full_compliance",
            },
        )

        assert response.status_code == 201
        client_org = db.get(Organization, response.json()["client_org_id"])
        assert client_org.name == "Sunrise Exports LLP"
        assert client_org.gstin_fingerprint == fingerprint("24ABCDE1234F1Z5")
        assert client_org.gstin != "24ABCDE1234F1Z5"

    def test_engaging_an_organization_already_on_the_platform(
        self, app_client, ca_firm, firm_admin, company
    ):
        response = app_client.post(
            f"{API}/clients", headers=auth(firm_admin), json={"client_org_id": company.id}
        )

        assert response.status_code == 201
        assert response.json()["client_org_id"] == company.id

    def test_neither_shape_is_a_validation_error(self, app_client, firm_admin):
        response = app_client.post(f"{API}/clients", headers=auth(firm_admin), json={})

        assert response.status_code == 422

    def test_both_shapes_at_once_is_a_validation_error(
        self, app_client, firm_admin, company
    ):
        response = app_client.post(
            f"{API}/clients",
            headers=auth(firm_admin),
            json={
                "client_org_id": company.id,
                "organization": {"name": "Sunrise Exports LLP"},
            },
        )

        assert response.status_code == 422

    def test_a_firm_cannot_engage_itself(self, app_client, ca_firm, firm_admin):
        response = app_client.post(
            f"{API}/clients", headers=auth(firm_admin), json={"client_org_id": ca_firm.id}
        )

        assert response.status_code == 422

    def test_engaging_an_unknown_organization_is_a_404(self, app_client, firm_admin):
        response = app_client.post(
            f"{API}/clients", headers=auth(firm_admin), json={"client_org_id": 999_999}
        )

        assert response.status_code == 404

    def test_a_duplicate_engagement_is_a_conflict_naming_the_existing_one(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        existing = make_engagement(db, ca_firm, company)

        response = app_client.post(
            f"{API}/clients", headers=auth(firm_admin), json={"client_org_id": company.id}
        )

        assert response.status_code == 409
        assert response.json()["error"]["details"]["client_id"] == existing.id

    def test_two_firms_may_engage_the_same_organization(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        """Which is what makes the data portable between firms."""
        make_engagement(db, ca_firm, company)
        second_firm = make_org(db, name="Rao & Co", type=OrgType.CA_FIRM)
        second_admin = make_user(db, second_firm, role=UserRole.ADMIN)

        response = app_client.post(
            f"{API}/clients", headers=auth(second_admin), json={"client_org_id": company.id}
        )

        assert response.status_code == 201

    def test_assigning_a_user_from_another_firm_is_refused(
        self, app_client, ca_firm, firm_admin, company, company_admin
    ):
        """Otherwise a user appears in the workload of an organization they
        have never heard of."""
        response = app_client.post(
            f"{API}/clients",
            headers=auth(firm_admin),
            json={"client_org_id": company.id, "assigned_user_id": company_admin.id},
        )

        assert response.status_code == 404

    def test_staff_cannot_engage_a_client(self, app_client, firm_staff, company):
        response = app_client.post(
            f"{API}/clients", headers=auth(firm_staff), json={"client_org_id": company.id}
        )

        assert response.status_code == 403

    def test_the_engagement_is_recorded_in_the_firms_chain_only(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        """Nothing has happened to the client's data yet; opening their trail
        with an entry they did not cause is noise."""
        from app.models.audit import AuditTrail

        app_client.post(
            f"{API}/clients", headers=auth(firm_admin), json={"client_org_id": company.id}
        )

        entries = db.query(AuditTrail).filter_by(action=AuditAction.CREATE).all()
        assert [e.organization_id for e in entries] == [ca_firm.id]


class TestClientDetail:
    def test_reading_one_engagement(self, app_client, db, ca_firm, firm_admin, company):
        engagement = make_engagement(db, ca_firm, company)

        response = app_client.get(f"{API}/clients/{engagement.id}", headers=auth(firm_admin))

        assert response.status_code == 200
        assert response.json()["client_organization"]["name"] == company.name

    def test_another_firms_engagement_is_a_404_not_a_403(
        self, app_client, db, ca_firm, company
    ):
        """403 would confirm the id exists and let one firm count a
        competitor's clients."""
        rival_firm = make_org(db, name="Rival & Co", type=OrgType.CA_FIRM)
        rival_admin = make_user(db, rival_firm, role=UserRole.ADMIN)
        engagement = make_engagement(db, ca_firm, company)

        response = app_client.get(f"{API}/clients/{engagement.id}", headers=auth(rival_admin))

        assert response.status_code == 404

    def test_unassigned_staff_get_a_404_on_a_client_of_their_own_firm(
        self, app_client, db, ca_firm, firm_staff, company
    ):
        engagement = make_engagement(db, ca_firm, company)

        response = app_client.get(f"{API}/clients/{engagement.id}", headers=auth(firm_staff))

        assert response.status_code == 404

    def test_assigned_staff_can_read_it(
        self, app_client, db, ca_firm, firm_staff, company
    ):
        engagement = make_engagement(db, ca_firm, company)
        make_assignment(db, firm_staff, engagement)

        response = app_client.get(f"{API}/clients/{engagement.id}", headers=auth(firm_staff))

        assert response.status_code == 200


class TestClientUpdate:
    def test_changing_the_engagement_terms(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        engagement = make_engagement(db, ca_firm, company)

        response = app_client.patch(
            f"{API}/clients/{engagement.id}",
            headers=auth(firm_admin),
            json={"engagement_type": "gst_only", "retainer_paise": 50_000_00},
        )

        assert response.status_code == 200
        assert response.json()["engagement_type"] == "gst_only"
        assert response.json()["retainer_paise"] == 50_000_00

    def test_terminating_sets_an_end_date_automatically(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        engagement = make_engagement(db, ca_firm, company)

        response = app_client.patch(
            f"{API}/clients/{engagement.id}",
            headers=auth(firm_admin),
            json={"status": "terminated"},
        )

        assert response.json()["end_date"] == date.today().isoformat()

    def test_terminating_cuts_off_an_outstanding_client_token(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        engagement = make_engagement(db, ca_firm, company)
        client_headers = auth(firm_admin, client_org_id=company.id)
        assert app_client.get(f"{API}/auth/me", headers=client_headers).status_code == 200

        app_client.patch(
            f"{API}/clients/{engagement.id}",
            headers=auth(firm_admin),
            json={"status": "terminated"},
        )

        assert app_client.get(f"{API}/auth/me", headers=client_headers).status_code == 403

    def test_an_explicit_null_clears_the_assignment(
        self, app_client, db, ca_firm, firm_admin, firm_staff, company
    ):
        """Which is how a client is returned to the unassigned queue."""
        engagement = make_engagement(db, ca_firm, company, assigned_user_id=firm_staff.id)

        response = app_client.patch(
            f"{API}/clients/{engagement.id}",
            headers=auth(firm_admin),
            json={"assigned_user_id": None},
        )

        assert response.json()["assigned_user_id"] is None

    def test_assigning_across_firms_is_refused(
        self, app_client, db, ca_firm, firm_admin, company, company_admin
    ):
        engagement = make_engagement(db, ca_firm, company)

        response = app_client.patch(
            f"{API}/clients/{engagement.id}",
            headers=auth(firm_admin),
            json={"assigned_user_id": company_admin.id},
        )

        assert response.status_code == 404

    def test_staff_cannot_update_an_engagement(
        self, app_client, db, ca_firm, firm_staff, company
    ):
        engagement = make_engagement(db, ca_firm, company)
        make_assignment(db, firm_staff, engagement)

        response = app_client.patch(
            f"{API}/clients/{engagement.id}",
            headers=auth(firm_staff),
            json={"notes": "trying"},
        )

        assert response.status_code == 403


class TestClientOffboarding:
    def test_ending_an_engagement_soft_deletes_it_and_leaves_the_client_intact(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        engagement = make_engagement(db, ca_firm, company)
        obligation = make_obligation(db)
        filing = make_filing(db, company, obligation)

        response = app_client.delete(
            f"{API}/clients/{engagement.id}", headers=auth(firm_admin)
        )

        assert response.status_code == 200
        db.refresh(engagement)
        db.refresh(company)
        db.refresh(filing)
        assert engagement.deleted_at is not None
        assert engagement.status == str(EngagementStatus.TERMINATED)
        assert company.deleted_at is None
        assert filing.deleted_at is None

    def test_the_assignments_go_with_it(
        self, app_client, db, ca_firm, firm_admin, firm_staff, company
    ):
        """Or a staff member keeps a live grant to a client the firm dropped."""
        engagement = make_engagement(db, ca_firm, company)
        assignment = make_assignment(db, firm_staff, engagement)

        app_client.delete(f"{API}/clients/{engagement.id}", headers=auth(firm_admin))

        db.refresh(assignment)
        assert assignment.deleted_at is not None

    def test_the_organization_can_be_engaged_by_another_firm_afterwards(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        engagement = make_engagement(db, ca_firm, company)
        app_client.delete(f"{API}/clients/{engagement.id}", headers=auth(firm_admin))

        second_firm = make_org(db, name="Rao & Co", type=OrgType.CA_FIRM)
        second_admin = make_user(db, second_firm, role=UserRole.ADMIN)

        response = app_client.post(
            f"{API}/clients", headers=auth(second_admin), json={"client_org_id": company.id}
        )

        assert response.status_code == 201

    def test_a_manager_cannot_offboard(
        self, app_client, db, ca_firm, company
    ):
        manager = make_user(db, ca_firm, role=UserRole.COMPLIANCE_MANAGER)
        engagement = make_engagement(db, ca_firm, company)

        response = app_client.delete(
            f"{API}/clients/{engagement.id}", headers=auth(manager)
        )

        assert response.status_code == 403


class TestAssignments:
    def test_assigning_a_staff_member(
        self, app_client, db, ca_firm, firm_admin, firm_staff, company
    ):
        engagement = make_engagement(db, ca_firm, company)

        response = app_client.post(
            f"{API}/clients/{engagement.id}/assignments",
            headers=auth(firm_admin),
            json={"user_id": firm_staff.id},
        )

        assert response.status_code == 201
        assert response.json()["user_id"] == firm_staff.id

    def test_the_assignment_is_what_unlocks_the_client_for_that_user(
        self, app_client, db, ca_firm, firm_admin, firm_staff, company
    ):
        engagement = make_engagement(db, ca_firm, company)
        assert (
            app_client.get(f"{API}/clients/{engagement.id}", headers=auth(firm_staff)).status_code
            == 404
        )

        app_client.post(
            f"{API}/clients/{engagement.id}/assignments",
            headers=auth(firm_admin),
            json={"user_id": firm_staff.id},
        )

        assert (
            app_client.get(f"{API}/clients/{engagement.id}", headers=auth(firm_staff)).status_code
            == 200
        )

    def test_a_grant_may_narrow_the_users_role(
        self, app_client, db, ca_firm, firm_admin, firm_staff, company
    ):
        engagement = make_engagement(db, ca_firm, company)

        response = app_client.post(
            f"{API}/clients/{engagement.id}/assignments",
            headers=auth(firm_admin),
            json={"user_id": firm_staff.id, "granted_role": "read_only"},
        )

        assert response.status_code == 201
        assert response.json()["granted_role"] == "read_only"

    def test_a_grant_that_would_widen_is_rejected_at_the_point_of_writing_it(
        self, app_client, db, ca_firm, firm_admin, firm_staff, company
    ):
        """``resolve_delegation`` caps it anyway; refusing here reports the
        mistake rather than silently ignoring it at request time."""
        engagement = make_engagement(db, ca_firm, company)

        response = app_client.post(
            f"{API}/clients/{engagement.id}/assignments",
            headers=auth(firm_admin),
            json={"user_id": firm_staff.id, "granted_role": "admin"},
        )

        assert response.status_code == 422
        assert response.json()["error"]["details"]["user_role"] == "staff"

    def test_an_unknown_role_name_is_rejected(
        self, app_client, db, ca_firm, firm_admin, firm_staff, company
    ):
        engagement = make_engagement(db, ca_firm, company)

        response = app_client.post(
            f"{API}/clients/{engagement.id}/assignments",
            headers=auth(firm_admin),
            json={"user_id": firm_staff.id, "granted_role": "superuser"},
        )

        assert response.status_code == 422

    def test_re_assigning_updates_the_grant_rather_than_duplicating_it(
        self, app_client, db, ca_firm, firm_admin, firm_staff, company
    ):
        engagement = make_engagement(db, ca_firm, company)
        body = {"user_id": firm_staff.id}
        app_client.post(
            f"{API}/clients/{engagement.id}/assignments", headers=auth(firm_admin), json=body
        )

        response = app_client.post(
            f"{API}/clients/{engagement.id}/assignments",
            headers=auth(firm_admin),
            json={**body, "granted_role": "read_only"},
        )

        assert response.status_code == 201
        listed = app_client.get(
            f"{API}/clients/{engagement.id}/assignments", headers=auth(firm_admin)
        ).json()
        assert len(listed) == 1
        assert listed[0]["granted_role"] == "read_only"

    def test_a_user_from_another_firm_cannot_be_assigned(
        self, app_client, db, ca_firm, firm_admin, company, company_admin
    ):
        engagement = make_engagement(db, ca_firm, company)

        response = app_client.post(
            f"{API}/clients/{engagement.id}/assignments",
            headers=auth(firm_admin),
            json={"user_id": company_admin.id},
        )

        assert response.status_code == 404

    def test_revoking_removes_the_access(
        self, app_client, db, ca_firm, firm_admin, firm_staff, company
    ):
        engagement = make_engagement(db, ca_firm, company)
        make_assignment(db, firm_staff, engagement)
        db.commit()

        response = app_client.delete(
            f"{API}/clients/{engagement.id}/assignments/{firm_staff.id}",
            headers=auth(firm_admin),
        )

        assert response.status_code == 200
        assert (
            app_client.get(f"{API}/clients/{engagement.id}", headers=auth(firm_staff)).status_code
            == 404
        )

    def test_revoking_a_missing_assignment_is_a_404(
        self, app_client, db, ca_firm, firm_admin, firm_staff, company
    ):
        engagement = make_engagement(db, ca_firm, company)

        response = app_client.delete(
            f"{API}/clients/{engagement.id}/assignments/{firm_staff.id}",
            headers=auth(firm_admin),
        )

        assert response.status_code == 404

    def test_assignment_changes_are_logged_as_role_changes(
        self, app_client, db, ca_firm, firm_admin, firm_staff, company
    ):
        from app.models.audit import AuditTrail

        engagement = make_engagement(db, ca_firm, company)
        app_client.post(
            f"{API}/clients/{engagement.id}/assignments",
            headers=auth(firm_admin),
            json={"user_id": firm_staff.id},
        )

        entry = db.query(AuditTrail).filter_by(action=AuditAction.ROLE_CHANGE).one()
        assert entry.entity_type == "client_assignment"

    def test_staff_cannot_manage_assignments(
        self, app_client, db, ca_firm, firm_staff, company
    ):
        engagement = make_engagement(db, ca_firm, company)
        make_assignment(db, firm_staff, engagement)

        response = app_client.get(
            f"{API}/clients/{engagement.id}/assignments", headers=auth(firm_staff)
        )

        assert response.status_code == 403


class TestEngagementOwnership:
    def test_an_engagement_row_belongs_to_the_firm_not_the_client(
        self, app_client, db, ca_firm, company, company_admin
    ):
        """A client cannot see or edit the engagement their CA holds on them."""
        engagement = make_engagement(db, ca_firm, company)

        response = app_client.get(
            f"{API}/clients/{engagement.id}", headers=auth(company_admin)
        )
        assert response.status_code == 403

    def test_a_terminated_engagement_still_appears_until_it_is_deleted(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        """Offboarding is a soft delete; the history stays visible to the firm."""
        engagement = make_engagement(db, ca_firm, company)
        engagement.status = str(EngagementStatus.TERMINATED)
        db.flush()

        response = app_client.get(f"{API}/clients", headers=auth(firm_admin))

        assert response.json()["total"] == 1

    def test_the_engagement_count_query_ignores_other_tenants_filings(
        self, app_client, db, ca_firm, firm_admin, company, other_company
    ):
        make_engagement(db, ca_firm, company)
        obligation = make_obligation(db)
        make_filing(db, other_company, obligation)
        db.flush()

        item = app_client.get(f"{API}/clients", headers=auth(firm_admin)).json()["items"][0]

        assert item["open_filings"] == 0


class TestClientModelIntegrity:
    def test_an_engagement_needs_both_sides(self, db, ca_firm, company):
        engagement = Client(
            ca_firm_id=ca_firm.id,
            client_org_id=company.id,
            engagement_type="full_compliance",
            status="active",
            start_date=date(2026, 4, 1),
        )
        db.add(engagement)
        db.flush()

        assert engagement.id is not None
