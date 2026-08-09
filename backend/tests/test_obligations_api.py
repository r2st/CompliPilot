"""The obligations catalogue and the per-organization applicability record.

Two resources that read as one, and the seam between them is where the bugs
live. ``/obligations`` is shared: system rows carry ``organization_id IS NULL``
and every tenant sees them, so the tests here check both halves of that — a
tenant sees the shared catalogue, and never another tenant's additions.
``/obligations/mine`` is the tenant's own record of what applies, including the
negatives.

The negatives matter more than they look. "GSTR-9 does not apply to you,
because your turnover is under ₹2 crore" is a thing a CA has to be able to show
a client during an assessment, so a list that returned only the positives could
not answer the question it exists to answer.
"""
from __future__ import annotations

from app.models.enums import AuditAction, Frequency, Regulation, UserRole
from app.models.obligation import ComplianceObligation, OrganizationObligation
from tests.conftest import (
    API,
    CRORE,
    auth,
    make_engagement,
    make_filing,
    make_obligation,
    make_user,
)


def _link(db, org, obligation, **kwargs) -> OrganizationObligation:
    """The row recording what one organization owes for one obligation."""
    link = OrganizationObligation(
        organization_id=org.id,
        obligation_id=obligation.id,
        **kwargs,
    )
    db.add(link)
    db.flush()
    return link


CUSTOM = {
    "regulation": "gst",
    "code": "acme.internal.gst.review",
    "title": "Internal GST reconciliation",
    "frequency": "monthly",
    "due_day": 15,
}


class TestCatalogueBrowsing:
    def test_the_system_catalogue_is_visible_to_every_tenant(
        self, app_client, seeded, company_admin, other_admin
    ):
        mine = app_client.get(f"{API}/obligations", headers=auth(company_admin)).json()
        theirs = app_client.get(f"{API}/obligations", headers=auth(other_admin)).json()

        assert mine["total"] > 0
        assert mine["total"] == theirs["total"]

    def test_a_tenants_own_obligation_is_invisible_to_another_tenant(
        self, app_client, db, company, company_admin, other_admin
    ):
        make_obligation(db, organization_id=company.id, code="acme.private")

        mine = app_client.get(f"{API}/obligations", headers=auth(company_admin)).json()
        theirs = app_client.get(f"{API}/obligations", headers=auth(other_admin)).json()

        assert "acme.private" in {i["code"] for i in mine["items"]}
        assert "acme.private" not in {i["code"] for i in theirs["items"]}

    def test_filtering_by_regulation(self, app_client, seeded, company_admin):
        response = app_client.get(
            f"{API}/obligations", headers=auth(company_admin), params={"regulation": "gst"}
        )

        assert response.json()["total"] > 0
        assert {i["regulation"] for i in response.json()["items"]} == {"gst"}

    def test_filtering_by_frequency(self, app_client, seeded, company_admin):
        response = app_client.get(
            f"{API}/obligations", headers=auth(company_admin), params={"frequency": "monthly"}
        )

        assert {i["frequency"] for i in response.json()["items"]} == {"monthly"}

    def test_searching_matches_the_title_the_code_and_the_form_number(
        self, app_client, db, company_admin
    ):
        make_obligation(db, code="findme.by.code", title="Nothing distinctive")
        make_obligation(db, code="other.one", title="Findme by title")
        make_obligation(db, code="third.one", title="Nothing", filing_type="FINDME-1")

        response = app_client.get(
            f"{API}/obligations", headers=auth(company_admin), params={"search": "findme"}
        )

        assert response.json()["total"] == 3

    def test_mine_only_excludes_the_shared_catalogue(
        self, app_client, db, seeded, company, company_admin
    ):
        make_obligation(db, organization_id=company.id, code="acme.private")

        response = app_client.get(
            f"{API}/obligations", headers=auth(company_admin), params={"mine_only": True}
        )

        assert response.json()["total"] == 1
        assert response.json()["items"][0]["code"] == "acme.private"

    def test_inactive_rows_are_hidden_unless_asked_for(
        self, app_client, db, company, company_admin
    ):
        make_obligation(db, organization_id=company.id, code="acme.retired", is_active=False)

        default = app_client.get(f"{API}/obligations", headers=auth(company_admin))
        assert default.json()["total"] == 0

        response = app_client.get(
            f"{API}/obligations", headers=auth(company_admin), params={"include_inactive": True}
        )
        assert response.json()["total"] == 1

    def test_soft_deleted_rows_are_never_returned(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db, organization_id=company.id)
        obligation.soft_delete()
        db.flush()

        response = app_client.get(
            f"{API}/obligations", headers=auth(company_admin), params={"include_inactive": True}
        )

        assert response.json()["total"] == 0

    def test_the_summary_counts_per_regulation(self, app_client, seeded, company_admin):
        response = app_client.get(f"{API}/obligations/summary", headers=auth(company_admin))

        body = response.json()
        assert body["total"] == sum(body["by_regulation"].values())
        assert "gst" in body["by_regulation"]

    def test_reading_one_obligation(self, app_client, db, company_admin):
        obligation = make_obligation(db, title="Monthly GST return")

        response = app_client.get(
            f"{API}/obligations/{obligation.id}", headers=auth(company_admin)
        )

        assert response.status_code == 200
        assert response.json()["title"] == "Monthly GST return"

    def test_another_tenants_obligation_is_a_404(
        self, app_client, db, other_company, company_admin
    ):
        obligation = make_obligation(db, organization_id=other_company.id)

        response = app_client.get(
            f"{API}/obligations/{obligation.id}", headers=auth(company_admin)
        )

        assert response.status_code == 404


class TestCustomObligations:
    def test_a_manager_can_add_one(self, app_client, db, company, company_admin):
        response = app_client.post(
            f"{API}/obligations", headers=auth(company_admin), json=CUSTOM
        )

        assert response.status_code == 201
        assert response.json()["is_system"] is False
        assert response.json()["organization_id"] == company.id

    def test_staff_cannot(self, app_client, company_staff):
        response = app_client.post(
            f"{API}/obligations", headers=auth(company_staff), json=CUSTOM
        )

        assert response.status_code == 403

    def test_a_duplicate_code_within_the_tenant_is_a_conflict(
        self, app_client, company_admin
    ):
        app_client.post(f"{API}/obligations", headers=auth(company_admin), json=CUSTOM)

        response = app_client.post(
            f"{API}/obligations", headers=auth(company_admin), json=CUSTOM
        )

        assert response.status_code == 409
        assert response.json()["error"]["details"]["code"] == CUSTOM["code"]

    def test_the_same_code_at_another_tenant_is_fine(
        self, app_client, company_admin, other_admin
    ):
        app_client.post(f"{API}/obligations", headers=auth(company_admin), json=CUSTOM)

        response = app_client.post(
            f"{API}/obligations", headers=auth(other_admin), json=CUSTOM
        )

        assert response.status_code == 201

    def test_a_code_colliding_with_a_system_row_is_allowed(
        self, app_client, db, seeded, company_admin
    ):
        """That is the override case, and the partial unique index is on
        ``(organization_id, code)`` precisely so it is permitted."""
        system_code = db.query(ComplianceObligation).filter_by(is_system=True).first().code

        response = app_client.post(
            f"{API}/obligations",
            headers=auth(company_admin),
            json={**CUSTOM, "code": system_code},
        )

        assert response.status_code == 201

    def test_two_due_date_rules_at_once_are_refused(self, app_client, company_admin):
        """Otherwise the engine takes whichever it tries first and the author
        has no way to find out which."""
        response = app_client.post(
            f"{API}/obligations",
            headers=auth(company_admin),
            json={**CUSTOM, "offset_days": 30},
        )

        assert response.status_code == 422

    def test_a_recurring_obligation_with_no_rule_is_refused(self, app_client, company_admin):
        payload = {k: v for k, v in CUSTOM.items() if k != "due_day"}

        response = app_client.post(
            f"{API}/obligations", headers=auth(company_admin), json=payload
        )

        assert response.status_code == 422

    def test_an_event_based_obligation_needs_an_offset(self, app_client, company_admin):
        response = app_client.post(
            f"{API}/obligations",
            headers=auth(company_admin),
            json={**CUSTOM, "frequency": "event_based", "due_day": None},
        )

        assert response.status_code == 422

    def test_an_inverted_turnover_band_is_refused(self, app_client, company_admin):
        response = app_client.post(
            f"{API}/obligations",
            headers=auth(company_admin),
            json={
                **CUSTOM,
                "min_turnover_paise": 10 * CRORE,
                "max_turnover_paise": 2 * CRORE,
            },
        )

        assert response.status_code == 422

    def test_an_uppercase_code_is_refused_by_the_pattern(self, app_client, company_admin):
        response = app_client.post(
            f"{API}/obligations", headers=auth(company_admin), json={**CUSTOM, "code": "ACME.X"}
        )

        assert response.status_code == 422

    def test_creation_is_audited(self, app_client, db, company_admin):
        from app.models.audit import AuditTrail

        app_client.post(f"{API}/obligations", headers=auth(company_admin), json=CUSTOM)

        entry = db.query(AuditTrail).filter_by(entity_type="compliance_obligation").one()
        assert entry.action == AuditAction.CREATE


class TestObligationEditing:
    def test_a_tenant_may_edit_their_own(self, app_client, db, company, company_admin):
        obligation = make_obligation(db, organization_id=company.id)

        response = app_client.patch(
            f"{API}/obligations/{obligation.id}",
            headers=auth(company_admin),
            json={"title": "Renamed"},
        )

        assert response.status_code == 200
        assert response.json()["title"] == "Renamed"

    def test_a_system_row_is_refused_with_an_explanation(
        self, app_client, db, seeded, company_admin
    ):
        """One tenant editing the shared GSTR-3B definition would change it for
        everyone."""
        system = db.query(ComplianceObligation).filter_by(is_system=True).first()

        response = app_client.patch(
            f"{API}/obligations/{system.id}",
            headers=auth(company_admin),
            json={"title": "Hijacked"},
        )

        assert response.status_code == 403
        assert "Create your own copy" in response.json()["error"]["message"]

    def test_another_tenants_obligation_is_a_404_not_a_403(
        self, app_client, db, other_company, company_admin
    ):
        obligation = make_obligation(db, organization_id=other_company.id)

        response = app_client.patch(
            f"{API}/obligations/{obligation.id}",
            headers=auth(company_admin),
            json={"title": "Renamed"},
        )

        assert response.status_code == 404

    def test_retiring_soft_deletes_and_deactivates(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db, organization_id=company.id)

        response = app_client.delete(
            f"{API}/obligations/{obligation.id}", headers=auth(company_admin)
        )

        assert response.status_code == 200
        db.refresh(obligation)
        assert obligation.deleted_at is not None
        assert obligation.is_active is False

    def test_retiring_takes_the_organization_links_with_it(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db, organization_id=company.id)
        link = _link(db, company, obligation, engine_verdict=True)

        app_client.delete(f"{API}/obligations/{obligation.id}", headers=auth(company_admin))

        db.refresh(link)
        assert link.deleted_at is not None

    def test_retiring_leaves_existing_filings_alone(
        self, app_client, db, company, company_admin
    ):
        """They still have to resolve their obligation at an assessment."""
        obligation = make_obligation(db, organization_id=company.id)
        filing = make_filing(db, company, obligation)

        app_client.delete(f"{API}/obligations/{obligation.id}", headers=auth(company_admin))

        db.refresh(filing)
        assert filing.deleted_at is None

    def test_a_system_row_cannot_be_retired(self, app_client, db, seeded, company_admin):
        system = db.query(ComplianceObligation).filter_by(is_system=True).first()

        response = app_client.delete(
            f"{API}/obligations/{system.id}", headers=auth(company_admin)
        )

        assert response.status_code == 403


class TestMyObligations:
    def test_the_list_carries_the_engine_verdict_and_the_obligation(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db, title="Monthly GST return")
        _link(db, company, obligation, engine_verdict=True, engine_reason="Turnover over ₹5cr")

        response = app_client.get(f"{API}/obligations/mine", headers=auth(company_admin))

        item = response.json()["items"][0]
        assert item["engine_verdict"] is True
        assert item["is_applicable"] is True
        assert item["obligation"]["title"] == "Monthly GST return"

    def test_the_negatives_are_returned_too(self, app_client, db, company, company_admin):
        """A CA has to be able to show a client why something does not apply."""
        applies = make_obligation(db, code="applies")
        does_not = make_obligation(db, code="does.not")
        _link(db, company, applies, engine_verdict=True)
        _link(db, company, does_not, engine_verdict=False, engine_reason="Turnover below ₹2cr")

        response = app_client.get(f"{API}/obligations/mine", headers=auth(company_admin))

        assert response.json()["total"] == 2

    def test_applicable_only_filters_on_the_effective_verdict(
        self, app_client, db, company, company_admin
    ):
        """Including the override, not just the engine's own answer."""
        engine_no_but_overridden_yes = make_obligation(db, code="overridden")
        engine_yes = make_obligation(db, code="engine.yes")
        engine_no = make_obligation(db, code="engine.no")
        _link(
            db,
            company,
            engine_no_but_overridden_yes,
            engine_verdict=False,
            is_applicable_override=True,
        )
        _link(db, company, engine_yes, engine_verdict=True)
        _link(db, company, engine_no, engine_verdict=False)

        response = app_client.get(
            f"{API}/obligations/mine",
            headers=auth(company_admin),
            params={"applicable_only": True},
        )

        assert response.json()["total"] == 2

    def test_an_override_of_false_beats_an_engine_yes(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        _link(db, company, obligation, engine_verdict=True, is_applicable_override=False)

        response = app_client.get(
            f"{API}/obligations/mine",
            headers=auth(company_admin),
            params={"applicable_only": True},
        )

        assert response.json()["total"] == 0

    def test_overridden_only_returns_the_human_decisions(
        self, app_client, db, company, company_admin
    ):
        overridden = make_obligation(db, code="overridden")
        untouched = make_obligation(db, code="untouched")
        _link(db, company, overridden, engine_verdict=True, is_applicable_override=False)
        _link(db, company, untouched, engine_verdict=True)

        response = app_client.get(
            f"{API}/obligations/mine",
            headers=auth(company_admin),
            params={"overridden_only": True},
        )

        assert response.json()["total"] == 1

    def test_filtering_by_regulation(self, app_client, db, company, company_admin):
        gst = make_obligation(db, code="a.gst", regulation=Regulation.GST)
        income_tax = make_obligation(db, code="a.it", regulation=Regulation.INCOME_TAX)
        _link(db, company, gst, engine_verdict=True)
        _link(db, company, income_tax, engine_verdict=True)

        response = app_client.get(
            f"{API}/obligations/mine",
            headers=auth(company_admin),
            params={"regulation": "income_tax"},
        )

        assert response.json()["total"] == 1

    def test_another_tenants_record_is_not_visible(
        self, app_client, db, other_company, company_admin
    ):
        obligation = make_obligation(db)
        _link(db, other_company, obligation, engine_verdict=True)

        response = app_client.get(f"{API}/obligations/mine", headers=auth(company_admin))

        assert response.json()["total"] == 0

    def test_a_firm_in_a_clients_context_sees_the_clients_record(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        make_engagement(db, ca_firm, company)
        obligation = make_obligation(db)
        _link(db, company, obligation, engine_verdict=True)

        response = app_client.get(
            f"{API}/obligations/mine", headers=auth(firm_admin, client_org_id=company.id)
        )

        assert response.json()["total"] == 1


class TestApplicabilityOverride:
    def test_a_manager_records_an_exemption(self, app_client, db, company, company_admin):
        obligation = make_obligation(db)
        link = _link(db, company, obligation, engine_verdict=True)

        response = app_client.patch(
            f"{API}/obligations/mine/{link.id}",
            headers=auth(company_admin),
            json={"is_applicable_override": False, "notes": "Exempt under notification 12/2024"},
        )

        assert response.status_code == 200
        assert response.json()["is_applicable_override"] is False
        assert response.json()["is_applicable"] is False

    def test_clearing_hands_the_question_back_to_the_engine(
        self, app_client, db, company, company_admin
    ):
        """A separate flag because Pydantic cannot tell "key absent" from
        "key sent as null", and those mean opposite things here."""
        obligation = make_obligation(db)
        link = _link(db, company, obligation, engine_verdict=True, is_applicable_override=False)

        response = app_client.patch(
            f"{API}/obligations/mine/{link.id}",
            headers=auth(company_admin),
            json={"clear_override": True},
        )

        assert response.json()["is_applicable_override"] is None
        assert response.json()["is_applicable"] is True

    def test_an_absent_key_leaves_the_override_alone(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        link = _link(db, company, obligation, engine_verdict=True, is_applicable_override=False)

        response = app_client.patch(
            f"{API}/obligations/mine/{link.id}",
            headers=auth(company_admin),
            json={"notes": "unrelated edit"},
        )

        assert response.json()["is_applicable_override"] is False

    def test_a_nightly_sync_does_not_undo_a_human_override(
        self, app_client, db, seeded, company, company_admin
    ):
        """The override is the whole point: a CA marking a client exempt has
        seen a fact the profile does not carry."""
        app_client.post(
            f"{API}/organizations/me/applicability/sync", headers=auth(company_admin)
        )
        link = (
            db.query(OrganizationObligation)
            .filter_by(organization_id=company.id, engine_verdict=True)
            .first()
        )
        app_client.patch(
            f"{API}/obligations/mine/{link.id}",
            headers=auth(company_admin),
            json={"is_applicable_override": False},
        )

        app_client.post(
            f"{API}/organizations/me/applicability/sync", headers=auth(company_admin)
        )

        db.refresh(link)
        assert link.is_applicable_override is False

    def test_the_owner_must_belong_to_this_organization_or_the_firm_acting_for_it(
        self, app_client, db, company, company_admin, other_admin
    ):
        obligation = make_obligation(db)
        link = _link(db, company, obligation, engine_verdict=True)

        response = app_client.patch(
            f"{API}/obligations/mine/{link.id}",
            headers=auth(company_admin),
            json={"owner_user_id": other_admin.id},
        )

        assert response.status_code == 404

    def test_a_firm_member_may_own_a_clients_obligation(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        make_engagement(db, ca_firm, company)
        obligation = make_obligation(db)
        link = _link(db, company, obligation, engine_verdict=True)

        response = app_client.patch(
            f"{API}/obligations/mine/{link.id}",
            headers=auth(firm_admin, client_org_id=company.id),
            json={"owner_user_id": firm_admin.id},
        )

        assert response.status_code == 200
        assert response.json()["owner_user_id"] == firm_admin.id

    def test_a_frequency_override_is_recorded(self, app_client, db, company, company_admin):
        obligation = make_obligation(db, frequency=Frequency.MONTHLY)
        link = _link(db, company, obligation, engine_verdict=True)

        response = app_client.patch(
            f"{API}/obligations/mine/{link.id}",
            headers=auth(company_admin),
            json={"frequency_override": "quarterly", "due_day_override": 22},
        )

        assert response.json()["frequency_override"] == "quarterly"
        assert response.json()["due_day_override"] == 22

    def test_a_due_day_outside_a_month_is_refused(
        self, app_client, db, company, company_admin
    ):
        obligation = make_obligation(db)
        link = _link(db, company, obligation, engine_verdict=True)

        response = app_client.patch(
            f"{API}/obligations/mine/{link.id}",
            headers=auth(company_admin),
            json={"due_day_override": 45},
        )

        assert response.status_code == 422

    def test_staff_cannot_override(self, app_client, db, company, company_staff):
        obligation = make_obligation(db)
        link = _link(db, company, obligation, engine_verdict=True)

        response = app_client.patch(
            f"{API}/obligations/mine/{link.id}",
            headers=auth(company_staff),
            json={"is_applicable_override": False},
        )

        assert response.status_code == 403

    def test_another_tenants_link_is_a_404(
        self, app_client, db, other_company, company_admin
    ):
        obligation = make_obligation(db)
        link = _link(db, other_company, obligation, engine_verdict=True)

        response = app_client.patch(
            f"{API}/obligations/mine/{link.id}",
            headers=auth(company_admin),
            json={"is_applicable_override": False},
        )

        assert response.status_code == 404

    def test_the_override_is_audited_with_the_before_and_after(
        self, app_client, db, company, company_admin
    ):
        from app.models.audit import AuditTrail

        obligation = make_obligation(db)
        link = _link(db, company, obligation, engine_verdict=True)

        app_client.patch(
            f"{API}/obligations/mine/{link.id}",
            headers=auth(company_admin),
            json={"is_applicable_override": False},
        )

        entry = db.query(AuditTrail).filter_by(entity_type="organization_obligation").one()
        assert entry.before_json["is_applicable_override"] is None
        assert entry.after_json["is_applicable_override"] is False

    def test_a_no_op_patch_writes_no_audit_entry(
        self, app_client, db, company, company_admin
    ):
        from app.models.audit import AuditTrail

        obligation = make_obligation(db)
        link = _link(db, company, obligation, engine_verdict=True, notes="unchanged")

        app_client.patch(
            f"{API}/obligations/mine/{link.id}",
            headers=auth(company_admin),
            json={"notes": "unchanged"},
        )

        assert db.query(AuditTrail).count() == 0


class TestReadOnlyAccess:
    def test_a_read_only_user_may_browse_but_not_write(
        self, app_client, db, company, company_reader
    ):
        obligation = make_obligation(db, organization_id=company.id)

        assert (
            app_client.get(f"{API}/obligations", headers=auth(company_reader)).status_code == 200
        )
        assert (
            app_client.patch(
                f"{API}/obligations/{obligation.id}",
                headers=auth(company_reader),
                json={"title": "Renamed"},
            ).status_code
            == 403
        )

    def test_a_manager_at_the_client_and_a_delegated_firm_manager_are_equivalent(
        self, app_client, db, ca_firm, company
    ):
        firm_manager = make_user(db, ca_firm, role=UserRole.COMPLIANCE_MANAGER)
        make_engagement(db, ca_firm, company)
        obligation = make_obligation(db)
        link = _link(db, company, obligation, engine_verdict=True)

        response = app_client.patch(
            f"{API}/obligations/mine/{link.id}",
            headers=auth(firm_manager, client_org_id=company.id),
            json={"is_applicable_override": False},
        )

        assert response.status_code == 200
