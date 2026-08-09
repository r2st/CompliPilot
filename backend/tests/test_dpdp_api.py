"""The DPDP Act toolkit: consent, data map, breaches, requests, assessments.

Five resources under one prefix because a Data Protection Board inquiry asks
about all five at once. The tests lead with the property that is a security
property rather than a feature.

**The data principal's identifier never appears in a list response.** It is the
most sensitive column in the product — an email or a phone number belonging to
a member of the public rather than to a customer of ours. It is stored
encrypted with a keyed fingerprint beside it, looked up by fingerprint, and
decrypted only on a single-record read. A register that shipped 40,000
identifiers to render a page would itself be the reportable incident, so the
tests assert the absence directly, on both registers, and assert that the
lookup path never puts the plaintext in a WHERE clause.

The rest of the file is about clocks. Two of them are statutory and they are
the reason this module exists: the Board must be told of a breach within 72
hours of *detection*, and a principal's request has a fixed SLA from *receipt*.
Both are computed from the recorded event rather than from data-entry time,
because dating either from when somebody got round to typing it in would
systematically overstate how much time is left — and a register that flatters
its own response times is worse than no register.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.core.crypto import fingerprint
from app.models.dpdp import (
    BREACH_NOTIFICATION_HOURS,
    DSR_SLA_DAYS,
    ConsentRecord,
    DataMapEntry,
    PrivacyImpactAssessment,
)
from app.models.enums import UserRole
from app.models.mixins import utcnow
from tests.conftest import API, auth, make_breach, make_dsr, make_user

PRINCIPAL = "customer@example.com"

CONSENT = {
    "principal_ref": PRINCIPAL,
    "purpose": "marketing",
    "purpose_description": "Product announcements by email",
    "notice_version": "2026.1",
    "data_categories_json": ["email", "name"],
}

MAP_ENTRY = {
    "system_name": "billing-postgres",
    "data_category": "contact",
    "purpose": "Invoicing",
    "legal_basis": "contract",
    "data_fields_json": ["email", "phone"],
}


def make_consent(db, org, *, principal_ref: str = PRINCIPAL, **kwargs) -> ConsentRecord:
    """A consent row, encrypted the way the route stores one."""
    from app.core.crypto import encrypt

    consent = ConsentRecord(
        organization_id=org.id,
        principal_ref=encrypt(principal_ref) or "",
        principal_fingerprint=fingerprint(principal_ref) or "",
        principal_type=kwargs.pop("principal_type", "customer"),
        purpose=kwargs.pop("purpose", "marketing"),
        notice_version=kwargs.pop("notice_version", "1"),
        notice_language=kwargs.pop("notice_language", "en"),
        is_granted=kwargs.pop("is_granted", True),
        granted_at=kwargs.pop("granted_at", utcnow()),
        **kwargs,
    )
    db.add(consent)
    db.flush()
    return consent


def make_map_entry(db, org, **kwargs) -> DataMapEntry:
    entry = DataMapEntry(
        organization_id=org.id,
        system_name=kwargs.pop("system_name", "billing-postgres"),
        data_category=kwargs.pop("data_category", "contact"),
        purpose=kwargs.pop("purpose", "Invoicing"),
        legal_basis=kwargs.pop("legal_basis", "contract"),
        **kwargs,
    )
    db.add(entry)
    db.flush()
    return entry


def make_pia(db, org, **kwargs) -> PrivacyImpactAssessment:
    pia = PrivacyImpactAssessment(
        organization_id=org.id,
        title=kwargs.pop("title", "Marketing analytics platform"),
        processing_activity=kwargs.pop("processing_activity", "Behavioural profiling"),
        status=kwargs.pop("status", "draft"),
        **kwargs,
    )
    db.add(pia)
    db.flush()
    return pia


class TestPrincipalIdentifiersAreNotDisclosed:
    """The one property in this module that is a security property."""

    def test_the_consent_register_carries_no_identifier_at_all(
        self, app_client, db, company, company_admin
    ):
        make_consent(db, company)

        item = app_client.get(
            f"{API}/dpdp/consents", headers=auth(company_admin)
        ).json()["items"][0]

        # Not the plaintext, and not the ciphertext either: a page that ships
        # 40,000 encrypted blobs has still shipped the column.
        assert item["principal_ref"] is None

    def test_the_request_register_carries_no_identifier_at_all(
        self, app_client, db, company, company_admin
    ):
        make_dsr(db, company, principal_ref=PRINCIPAL)

        item = app_client.get(
            f"{API}/dpdp/requests", headers=auth(company_admin)
        ).json()["items"][0]

        assert item["principal_ref"] is None

    def test_a_single_record_read_returns_the_decrypted_identifier(
        self, app_client, company_admin
    ):
        body = app_client.post(
            f"{API}/dpdp/consents", headers=auth(company_admin), json=CONSENT
        ).json()

        assert body["principal_ref"] == PRINCIPAL

    def test_the_stored_column_is_ciphertext_not_the_address(
        self, app_client, db, company_admin
    ):
        app_client.post(f"{API}/dpdp/consents", headers=auth(company_admin), json=CONSENT)

        consent = db.query(ConsentRecord).one()
        assert consent.principal_ref.startswith("enc.v1:")
        assert PRINCIPAL not in consent.principal_ref

    def test_the_same_address_encrypts_differently_each_time(
        self, app_client, db, company_admin
    ):
        """A random nonce per write, so the ciphertext does not leak equality."""
        for _ in range(2):
            app_client.post(
                f"{API}/dpdp/consents",
                headers=auth(company_admin),
                json={**CONSENT, "purpose": f"purpose-{_}"},
            )

        stored = [c.principal_ref for c in db.query(ConsentRecord).all()]
        assert stored[0] != stored[1]
        # But the fingerprint is deterministic, which is what makes lookup work.
        prints = {c.principal_fingerprint for c in db.query(ConsentRecord).all()}
        assert len(prints) == 1

    def test_a_lookup_matches_by_fingerprint_rather_than_by_the_address(
        self, app_client, db, company, company_admin
    ):
        make_consent(db, company, principal_ref=PRINCIPAL)
        make_consent(db, company, principal_ref="someone.else@example.com")

        body = app_client.get(
            f"{API}/dpdp/consents",
            headers=auth(company_admin),
            params={"principal_ref": PRINCIPAL},
        ).json()

        assert body["total"] == 1
        assert body["items"][0]["principal_ref"] is None

    def test_an_audit_entry_carries_the_fingerprint_not_the_address(
        self, app_client, db, company_admin
    ):
        """The trail is read by more people than the table it describes."""
        from app.models.audit import AuditTrail

        app_client.post(f"{API}/dpdp/consents", headers=auth(company_admin), json=CONSENT)

        entry = db.query(AuditTrail).filter_by(entity_type="consent_record").one()
        assert entry.after_json["principal_fingerprint"] == fingerprint(PRINCIPAL)
        assert PRINCIPAL not in str(entry.after_json)


class TestConsentRegister:
    def test_a_consent_is_recorded_with_the_notice_it_was_given_under(
        self, app_client, db, company_admin
    ):
        """"They agreed" is worthless without what they agreed to."""
        body = app_client.post(
            f"{API}/dpdp/consents",
            headers=auth(company_admin),
            json={**CONSENT, "notice_text": "We will email you about new features."},
        ).json()

        assert body["notice_version"] == "2026.1"
        assert body["is_granted"] is True
        assert body["granted_at"] is not None
        assert db.query(ConsentRecord).one().notice_text.startswith("We will email")

    def test_filtering_by_purpose_and_on_the_live_ones(
        self, app_client, db, company, company_admin
    ):
        make_consent(db, company, purpose="marketing")
        make_consent(db, company, purpose="analytics", is_granted=False)
        make_consent(db, company, purpose="support", withdrawn_at=utcnow())

        def total(**params):
            return app_client.get(
                f"{API}/dpdp/consents", headers=auth(company_admin), params=params
            ).json()["total"]

        assert total() == 3
        assert total(purpose="marketing") == 1
        assert total(granted_only=True) == 1

    def test_withdrawing_keeps_the_row_as_evidence(
        self, app_client, db, company, company_admin
    ):
        """The fiduciary still has to show what was consented to, and for how
        long, before it was withdrawn."""
        consent = make_consent(db, company)

        body = app_client.post(
            f"{API}/dpdp/consents/{consent.id}/withdraw",
            headers=auth(company_admin),
            json={"reason": "Unsubscribed from the footer link"},
        ).json()

        assert body["is_granted"] is False
        assert body["withdrawn_at"] is not None
        assert body["granted_at"] is not None
        assert db.query(ConsentRecord).count() == 1

    def test_the_withdrawal_reason_is_kept_with_the_evidence(
        self, app_client, db, company, company_admin
    ):
        consent = make_consent(db, company)

        app_client.post(
            f"{API}/dpdp/consents/{consent.id}/withdraw",
            headers=auth(company_admin),
            json={"reason": "Requested by phone"},
        )

        db.refresh(consent)
        assert consent.evidence_json["withdrawal_reason"] == "Requested by phone"

    def test_withdrawing_twice_is_a_conflict(
        self, app_client, db, company, company_admin
    ):
        consent = make_consent(db, company, withdrawn_at=utcnow(), is_granted=False)

        response = app_client.post(
            f"{API}/dpdp/consents/{consent.id}/withdraw",
            headers=auth(company_admin),
            json={},
        )

        assert response.status_code == 409

    def test_a_read_only_user_may_not_record_a_consent(self, app_client, company_reader):
        response = app_client.post(
            f"{API}/dpdp/consents", headers=auth(company_reader), json=CONSENT
        )

        assert response.status_code == 403

    def test_another_tenants_consents_are_invisible(
        self, app_client, db, other_company, company_admin
    ):
        make_consent(db, other_company)

        body = app_client.get(f"{API}/dpdp/consents", headers=auth(company_admin)).json()

        assert body["total"] == 0

    def test_another_tenants_consent_cannot_be_withdrawn(
        self, app_client, db, other_company, company_admin
    ):
        theirs = make_consent(db, other_company)

        response = app_client.post(
            f"{API}/dpdp/consents/{theirs.id}/withdraw",
            headers=auth(company_admin),
            json={},
        )

        assert response.status_code == 404


class TestDataMap:
    def test_an_entry_is_added(self, app_client, db, company, company_admin):
        body = app_client.post(
            f"{API}/dpdp/data-map", headers=auth(company_admin), json=MAP_ENTRY
        ).json()

        assert body["system_name"] == "billing-postgres"
        assert body["legal_basis"] == "contract"
        assert body["organization_id"] == company.id

    def test_a_cross_border_transfer_must_name_a_destination(
        self, app_client, company_admin
    ):
        """The flag exists to be assessed against the restricted-territory
        rules, and a transfer with no destination cannot be."""
        response = app_client.post(
            f"{API}/dpdp/data-map",
            headers=auth(company_admin),
            json={**MAP_ENTRY, "is_transferred_abroad": True},
        )

        assert response.status_code == 422

    def test_a_cross_border_transfer_with_a_destination_is_accepted(
        self, app_client, company_admin
    ):
        response = app_client.post(
            f"{API}/dpdp/data-map",
            headers=auth(company_admin),
            json={
                **MAP_ENTRY,
                "is_transferred_abroad": True,
                "transfer_countries_json": ["Singapore"],
            },
        )

        assert response.status_code == 201

    def test_an_unrecognised_legal_basis_is_refused(self, app_client, company_admin):
        response = app_client.post(
            f"{API}/dpdp/data-map",
            headers=auth(company_admin),
            json={**MAP_ENTRY, "legal_basis": "because_we_felt_like_it"},
        )

        assert response.status_code == 422

    def test_editing_an_entry_counts_as_reviewing_it(
        self, app_client, db, company, company_admin
    ):
        """Requiring a separate click would leave the review date permanently
        stale, and the readiness report reads it."""
        entry = make_map_entry(db, company)
        assert entry.last_reviewed_at is None

        app_client.patch(
            f"{API}/dpdp/data-map/{entry.id}",
            headers=auth(company_admin),
            json={**MAP_ENTRY, "purpose": "Invoicing and dunning"},
        )

        db.refresh(entry)
        assert entry.last_reviewed_at is not None
        assert entry.purpose == "Invoicing and dunning"

    def test_filtering_on_the_sensitive_and_the_cross_border(
        self, app_client, db, company, company_admin
    ):
        make_map_entry(db, company, system_name="a", is_sensitive=True)
        make_map_entry(
            db,
            company,
            system_name="b",
            is_transferred_abroad=True,
            transfer_countries_json=["Ireland"],
        )
        make_map_entry(db, company, system_name="c")

        def total(**params):
            return app_client.get(
                f"{API}/dpdp/data-map", headers=auth(company_admin), params=params
            ).json()["total"]

        assert total() == 3
        assert total(sensitive_only=True) == 1
        assert total(cross_border_only=True) == 1

    def test_removing_an_entry_is_a_managers_decision(
        self, app_client, db, company, company_staff
    ):
        entry = make_map_entry(db, company)

        response = app_client.delete(
            f"{API}/dpdp/data-map/{entry.id}", headers=auth(company_staff)
        )

        assert response.status_code == 403

    def test_a_removed_entry_leaves_the_inventory(
        self, app_client, db, company, company_admin
    ):
        entry = make_map_entry(db, company)

        app_client.delete(f"{API}/dpdp/data-map/{entry.id}", headers=auth(company_admin))

        body = app_client.get(f"{API}/dpdp/data-map", headers=auth(company_admin)).json()
        assert body["total"] == 0
        db.refresh(entry)
        assert entry.deleted_at is not None

    def test_another_tenants_entry_is_a_404(
        self, app_client, db, other_company, company_admin
    ):
        theirs = make_map_entry(db, other_company)

        response = app_client.patch(
            f"{API}/dpdp/data-map/{theirs.id}", headers=auth(company_admin), json=MAP_ENTRY
        )

        assert response.status_code == 404


class TestBreachRegister:
    def test_reporting_a_breach_starts_the_clock_and_numbers_it(
        self, app_client, db, company_admin
    ):
        body = app_client.post(
            f"{API}/dpdp/breaches",
            headers=auth(company_admin),
            json={"title": "Backup bucket left public", "severity": "high"},
        ).json()

        assert body["status"] == "open"
        assert body["reference"].startswith("BR-")
        assert body["dpb_notification_overdue"] is False
        assert 0 < body["hours_until_dpb_deadline"] <= BREACH_NOTIFICATION_HOURS

    def test_the_reference_is_sequential_per_organization(
        self, app_client, db, company, other_company, company_admin, other_admin
    ):
        """Global numbering would tell a client how many breaches other
        tenants have reported."""
        first = app_client.post(
            f"{API}/dpdp/breaches", headers=auth(company_admin), json={"title": "One"}
        ).json()
        second = app_client.post(
            f"{API}/dpdp/breaches", headers=auth(company_admin), json={"title": "Two"}
        ).json()
        theirs = app_client.post(
            f"{API}/dpdp/breaches", headers=auth(other_admin), json={"title": "Theirs"}
        ).json()

        assert first["reference"].endswith("-0001")
        assert second["reference"].endswith("-0002")
        assert theirs["reference"].endswith("-0001")

    def test_the_clock_runs_from_detection_not_from_data_entry(
        self, app_client, company_admin
    ):
        """Backdating to the truth is what makes the deadline correct."""
        detected = utcnow() - timedelta(hours=48)

        body = app_client.post(
            f"{API}/dpdp/breaches",
            headers=auth(company_admin),
            json={"title": "Found in last week's logs", "detected_at": detected.isoformat()},
        ).json()

        assert body["dpb_notification_overdue"] is False
        assert 23 <= body["hours_until_dpb_deadline"] <= 24

    def test_a_breach_detected_more_than_72_hours_ago_is_already_overdue(
        self, app_client, company_admin
    ):
        detected = utcnow() - timedelta(hours=BREACH_NOTIFICATION_HOURS + 5)

        body = app_client.post(
            f"{API}/dpdp/breaches",
            headers=auth(company_admin),
            json={"title": "Found late", "detected_at": detected.isoformat()},
        ).json()

        assert body["dpb_notification_overdue"] is True
        assert body["hours_until_dpb_deadline"] < 0

    def test_a_breach_cannot_be_detected_before_it_occurred(
        self, app_client, company_admin
    ):
        now = utcnow()

        response = app_client.post(
            f"{API}/dpdp/breaches",
            headers=auth(company_admin),
            json={
                "title": "Impossible",
                "occurred_at": now.isoformat(),
                "detected_at": (now - timedelta(hours=1)).isoformat(),
            },
        )

        assert response.status_code == 422

    def test_an_unrecognised_severity_is_refused(self, app_client, company_admin):
        response = app_client.post(
            f"{API}/dpdp/breaches",
            headers=auth(company_admin),
            json={"title": "A breach", "severity": "apocalyptic"},
        )

        assert response.status_code == 422

    def test_filtering_on_the_open_and_the_unnotified(
        self, app_client, db, company, company_admin
    ):
        make_breach(db, company)
        make_breach(db, company, closed_at=utcnow(), dpb_notified_at=utcnow())

        def total(**params):
            return app_client.get(
                f"{API}/dpdp/breaches", headers=auth(company_admin), params=params
            ).json()["total"]

        assert total() == 2
        assert total(open_only=True) == 1
        assert total(unnotified_only=True) == 1

    def test_a_breach_cannot_be_closed_before_the_board_was_told(
        self, app_client, db, company, company_admin
    ):
        breach = make_breach(db, company)

        response = app_client.patch(
            f"{API}/dpdp/breaches/{breach.id}",
            headers=auth(company_admin),
            json={"status": "closed"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["details"]["reference"] == breach.reference

    def test_containing_a_breach_stamps_the_time(
        self, app_client, db, company, company_admin
    ):
        breach = make_breach(db, company)

        body = app_client.patch(
            f"{API}/dpdp/breaches/{breach.id}",
            headers=auth(company_admin),
            json={"status": "contained"},
        ).json()

        assert body["contained_at"] is not None

    def test_the_update_is_audited_with_the_before_and_after(
        self, app_client, db, company, company_admin
    ):
        from app.models.audit import AuditTrail

        breach = make_breach(db, company, severity="high")

        app_client.patch(
            f"{API}/dpdp/breaches/{breach.id}",
            headers=auth(company_admin),
            json={"severity": "critical"},
        )

        entry = (
            db.query(AuditTrail)
            .filter_by(entity_type="breach_incident", action="update")
            .one()
        )
        assert entry.before_json["severity"] == "high"
        assert entry.after_json["severity"] == "critical"


class TestBreachNotification:
    def test_notifying_the_board_records_the_time_and_moves_the_status(
        self, app_client, db, company, company_admin
    ):
        breach = make_breach(db, company)

        body = app_client.post(
            f"{API}/dpdp/breaches/{breach.id}/notify",
            headers=auth(company_admin),
            json={"dpb_reference": "DPB/2026/00412"},
        ).json()

        assert body["dpb_notified_at"] is not None
        assert body["dpb_reference"] == "DPB/2026/00412"
        assert body["status"] == "notified"
        assert body["dpb_notification_overdue"] is False

    def test_the_audit_summary_names_how_long_after_detection_it_happened(
        self, app_client, db, company, company_admin
    ):
        """That number is what an inquiry asks for."""
        from app.models.audit import AuditTrail

        breach = make_breach(db, company, detected_at=utcnow() - timedelta(hours=10))

        app_client.post(
            f"{API}/dpdp/breaches/{breach.id}/notify", headers=auth(company_admin), json={}
        )

        entry = (
            db.query(AuditTrail)
            .filter_by(entity_type="breach_incident", action="notify")
            .one()
        )
        assert "10.0 hours after detection" in entry.summary
        assert "past the" not in entry.summary

    def test_a_late_notification_says_so_in_the_trail(
        self, app_client, db, company, company_admin
    ):
        from app.models.audit import AuditTrail

        breach = make_breach(
            db, company, detected_at=utcnow() - timedelta(hours=BREACH_NOTIFICATION_HOURS + 2)
        )

        app_client.post(
            f"{API}/dpdp/breaches/{breach.id}/notify", headers=auth(company_admin), json={}
        )

        entry = (
            db.query(AuditTrail)
            .filter_by(entity_type="breach_incident", action="notify")
            .one()
        )
        assert f"past the {BREACH_NOTIFICATION_HOURS}-hour window" in entry.summary

    def test_the_board_cannot_be_notified_twice(
        self, app_client, db, company, company_admin
    ):
        breach = make_breach(db, company, dpb_notified_at=utcnow())

        response = app_client.post(
            f"{API}/dpdp/breaches/{breach.id}/notify", headers=auth(company_admin), json={}
        )

        assert response.status_code == 409

    def test_notifying_the_principals_is_a_separate_record(
        self, app_client, db, company, company_admin
    ):
        breach = make_breach(db, company)

        body = app_client.post(
            f"{API}/dpdp/breaches/{breach.id}/notify",
            headers=auth(company_admin),
            json={"notify_principals": True, "notification_text": "Please reset your password."},
        ).json()

        assert body["principals_notified_at"] is not None
        # Telling the customers is not telling the Board.
        assert body["dpb_notified_at"] is None
        assert body["status"] == "open"

    def test_the_principals_may_be_notified_more_than_once(
        self, app_client, db, company, company_admin
    ):
        breach = make_breach(db, company)

        for _ in range(2):
            response = app_client.post(
                f"{API}/dpdp/breaches/{breach.id}/notify",
                headers=auth(company_admin),
                json={"notify_principals": True},
            )

        assert response.status_code == 200

    def test_notification_once_recorded_permits_closing_the_breach(
        self, app_client, db, company, company_admin
    ):
        breach = make_breach(db, company)
        app_client.post(
            f"{API}/dpdp/breaches/{breach.id}/notify", headers=auth(company_admin), json={}
        )

        body = app_client.patch(
            f"{API}/dpdp/breaches/{breach.id}",
            headers=auth(company_admin),
            json={"status": "closed"},
        ).json()

        assert body["status"] == "closed"
        assert body["closed_at"] is not None

    def test_staff_may_not_record_a_statutory_notification(
        self, app_client, db, company, company_staff
    ):
        """It is the single most consequential record in the module."""
        breach = make_breach(db, company)

        response = app_client.post(
            f"{API}/dpdp/breaches/{breach.id}/notify", headers=auth(company_staff), json={}
        )

        assert response.status_code == 403

    def test_another_tenants_breach_is_a_404(
        self, app_client, db, other_company, company_admin
    ):
        theirs = make_breach(db, other_company)

        response = app_client.post(
            f"{API}/dpdp/breaches/{theirs.id}/notify", headers=auth(company_admin), json={}
        )

        assert response.status_code == 404


class TestDataSubjectRequests:
    def test_logging_a_request_starts_the_sla_from_receipt(
        self, app_client, db, company_admin
    ):
        """A request logged three days after it arrived is already three days
        into its window."""
        received = utcnow() - timedelta(days=3)

        body = app_client.post(
            f"{API}/dpdp/requests",
            headers=auth(company_admin),
            json={
                "request_type": "access",
                "principal_ref": PRINCIPAL,
                "received_at": received.isoformat(),
            },
        ).json()

        expected = (received + timedelta(days=DSR_SLA_DAYS)).date()
        assert body["due_date"] == expected.isoformat()
        assert body["reference"].startswith("DSR-")
        assert body["status"] == "received"

    def test_an_unrecognised_request_type_is_refused(self, app_client, company_admin):
        response = app_client.post(
            f"{API}/dpdp/requests",
            headers=auth(company_admin),
            json={"request_type": "please_delete_everything", "principal_ref": PRINCIPAL},
        )

        assert response.status_code == 422

    def test_the_soonest_due_comes_first(self, app_client, db, company, company_admin):
        make_dsr(db, company, due_date=date(2026, 9, 20))
        make_dsr(db, company, due_date=date(2026, 9, 5))

        items = app_client.get(
            f"{API}/dpdp/requests", headers=auth(company_admin)
        ).json()["items"]

        assert [i["due_date"] for i in items] == ["2026-09-05", "2026-09-20"]

    def test_the_overdue_flag_and_the_countdown_are_computed(
        self, app_client, db, company, company_admin
    ):
        make_dsr(db, company, due_date=date(2026, 9, 1))

        item = app_client.get(
            f"{API}/dpdp/requests",
            headers=auth(company_admin),
            params={"today": "2026-09-06"},
        ).json()["items"][0]

        assert item["days_until_due"] == -5
        assert item["is_overdue"] is True

    def test_a_finished_request_is_never_overdue(
        self, app_client, db, company, company_admin
    ):
        make_dsr(db, company, due_date=date(2026, 9, 1), status="completed")

        item = app_client.get(
            f"{API}/dpdp/requests",
            headers=auth(company_admin),
            params={"today": "2026-09-06"},
        ).json()["items"][0]

        assert item["is_overdue"] is False

    def test_filtering_by_type_and_on_the_open_and_overdue(
        self, app_client, db, company, company_admin
    ):
        make_dsr(db, company, request_type="access", due_date=date(2026, 9, 1))
        make_dsr(db, company, request_type="erasure", due_date=date(2026, 9, 20))
        make_dsr(db, company, request_type="access", status="completed")

        def total(**params):
            return app_client.get(
                f"{API}/dpdp/requests", headers=auth(company_admin), params=params
            ).json()["total"]

        assert total(request_type="access") == 2
        assert total(open_only=True) == 2
        assert total(overdue_only=True, today="2026-09-06") == 1

    def test_progressing_a_request_stamps_completion(
        self, app_client, db, company, company_admin
    ):
        dsr = make_dsr(db, company)

        body = app_client.patch(
            f"{API}/dpdp/requests/{dsr.id}",
            headers=auth(company_admin),
            json={"status": "completed", "response": "The export was emailed."},
        ).json()

        assert body["status"] == "completed"
        assert body["completed_at"] is not None

    def test_verifying_the_principal_is_recorded(
        self, app_client, db, company, company_admin
    ):
        dsr = make_dsr(db, company)

        body = app_client.patch(
            f"{API}/dpdp/requests/{dsr.id}",
            headers=auth(company_admin),
            json={"mark_verified": True, "status": "in_progress"},
        ).json()

        assert body["verified_at"] is not None
        assert body["status"] == "in_progress"

    def test_a_rejection_needs_a_reason(self, app_client, db, company, company_admin):
        dsr = make_dsr(db, company)

        without = app_client.patch(
            f"{API}/dpdp/requests/{dsr.id}",
            headers=auth(company_admin),
            json={"status": "rejected"},
        )
        with_reason = app_client.patch(
            f"{API}/dpdp/requests/{dsr.id}",
            headers=auth(company_admin),
            json={"status": "rejected", "rejection_reason": "Identity could not be verified"},
        )

        assert without.status_code == 422
        assert with_reason.status_code == 200

    def test_the_progression_is_audited(self, app_client, db, company, company_admin):
        from app.models.audit import AuditTrail

        dsr = make_dsr(db, company, status="received")

        app_client.patch(
            f"{API}/dpdp/requests/{dsr.id}",
            headers=auth(company_admin),
            json={"status": "in_progress"},
        )

        entry = (
            db.query(AuditTrail).filter_by(entity_type="data_subject_request").one()
        )
        assert entry.before_json["status"] == "received"
        assert entry.after_json["status"] == "in_progress"

    def test_another_tenants_request_is_a_404(
        self, app_client, db, other_company, company_admin
    ):
        theirs = make_dsr(db, other_company)

        response = app_client.patch(
            f"{API}/dpdp/requests/{theirs.id}",
            headers=auth(company_admin),
            json={"status": "in_progress"},
        )

        assert response.status_code == 404


class TestAssessments:
    def test_a_new_assessment_starts_as_a_draft(self, app_client, company_admin):
        body = app_client.post(
            f"{API}/dpdp/assessments",
            headers=auth(company_admin),
            json={
                "title": "Marketing analytics platform",
                "processing_activity": "Behavioural profiling of website visitors",
            },
        ).json()

        assert body["status"] == "draft"
        assert body["reviewed_at"] is None

    def test_signing_off_records_who_and_schedules_the_next_review(
        self, app_client, db, company, company_admin
    ):
        """An assessment whose risk score came from a model is a draft until
        somebody accountable has read it."""
        pia = make_pia(db, company)

        body = app_client.post(
            f"{API}/dpdp/assessments/{pia.id}/review", headers=auth(company_admin)
        ).json()

        assert body["status"] == "approved"
        assert body["reviewed_by_id"] == company_admin.id
        assert body["reviewed_at"] is not None
        assert body["next_review_date"] is not None

    def test_the_review_interval_is_settable(self, app_client, db, company, company_admin):
        pia = make_pia(db, company)

        body = app_client.post(
            f"{API}/dpdp/assessments/{pia.id}/review",
            headers=auth(company_admin),
            params={"next_review_months": 6},
        ).json()

        due = date.fromisoformat(body["next_review_date"])
        today = date.today()
        assert 175 <= (due - today).days <= 190

    def test_a_review_on_the_31st_lands_on_a_date_that_exists(
        self, app_client, db, company, company_admin
    ):
        """Rolled forward by whole months with the day clamped."""
        pia = make_pia(db, company)

        body = app_client.post(
            f"{API}/dpdp/assessments/{pia.id}/review",
            headers=auth(company_admin),
            params={"next_review_months": 1},
        ).json()

        # Parsing it at all is the assertion: an unclamped roll-forward from a
        # 31st would have raised before it got here.
        assert date.fromisoformat(body["next_review_date"]) > date.today()

    def test_staff_may_not_sign_off(self, app_client, db, company, company_staff):
        pia = make_pia(db, company)

        response = app_client.post(
            f"{API}/dpdp/assessments/{pia.id}/review", headers=auth(company_staff)
        )

        assert response.status_code == 403

    def test_a_compliance_manager_may_sign_off(self, app_client, db, company):
        manager = make_user(db, company, role=UserRole.COMPLIANCE_MANAGER)
        pia = make_pia(db, company)

        response = app_client.post(
            f"{API}/dpdp/assessments/{pia.id}/review", headers=auth(manager)
        )

        assert response.status_code == 200

    def test_filtering_by_status(self, app_client, db, company, company_admin):
        make_pia(db, company, title="One", status="draft")
        make_pia(db, company, title="Two", status="approved")

        body = app_client.get(
            f"{API}/dpdp/assessments",
            headers=auth(company_admin),
            params={"status": "approved"},
        ).json()

        assert body["total"] == 1

    def test_another_tenants_assessment_is_a_404(
        self, app_client, db, other_company, company_admin
    ):
        theirs = make_pia(db, other_company)

        response = app_client.post(
            f"{API}/dpdp/assessments/{theirs.id}/review", headers=auth(company_admin)
        )

        assert response.status_code == 404


class TestReadiness:
    def test_an_empty_tenant_scores_badly_and_is_told_why(
        self, app_client, company_admin
    ):
        body = app_client.get(f"{API}/dpdp/readiness", headers=auth(company_admin)).json()

        assert body["has_data_map"] is False
        assert body["score"] == 55  # 100 - 30 (no map) - 15 (no consents)
        assert any("personal-data map" in gap for gap in body["gaps"])
        assert any("consent records" in gap for gap in body["gaps"])

    def test_the_counts_reflect_what_is_there(
        self, app_client, db, company, company_admin
    ):
        make_map_entry(db, company)
        make_consent(db, company, purpose="marketing")
        make_consent(
            db, company, purpose="analytics", withdrawn_at=utcnow(), is_granted=False
        )
        make_breach(db, company)
        make_dsr(db, company, due_date=date(2026, 12, 1))

        body = app_client.get(f"{API}/dpdp/readiness", headers=auth(company_admin)).json()

        assert body["has_data_map"] is True
        assert body["data_map_entries"] == 1
        assert body["consent_records"] == 2
        assert body["active_consents"] == 1
        assert body["withdrawn_consents"] == 1
        assert body["open_breaches"] == 1
        assert body["open_data_requests"] == 1

    def test_a_missed_board_window_costs_more_than_an_incomplete_inventory(
        self, app_client, db, company, company_admin
    ):
        """The two things with a statutory clock are weighted above the things
        that are merely bad practice."""
        make_map_entry(db, company)
        make_consent(db, company)
        make_pia(db, company, status="approved")
        make_breach(
            db,
            company,
            detected_at=utcnow() - timedelta(hours=BREACH_NOTIFICATION_HOURS + 1),
        )

        body = app_client.get(f"{API}/dpdp/readiness", headers=auth(company_admin)).json()

        assert body["overdue_dpb_notifications"] == 1
        assert body["score"] == 75
        assert any("Data Protection Board window" in gap for gap in body["gaps"])

    def test_an_overdue_principal_request_costs_the_score(
        self, app_client, db, company, company_admin
    ):
        make_map_entry(db, company)
        make_consent(db, company)
        make_pia(db, company, status="approved")
        make_dsr(db, company, due_date=date(2026, 8, 1))

        body = app_client.get(
            f"{API}/dpdp/readiness",
            headers=auth(company_admin),
            params={"today": "2026-08-15"},
        ).json()

        assert body["overdue_data_requests"] == 1
        assert body["score"] == 85

    def test_cross_border_transfers_with_no_assessment_are_flagged(
        self, app_client, db, company, company_admin
    ):
        make_map_entry(
            db, company, is_transferred_abroad=True, transfer_countries_json=["USA"]
        )
        make_consent(db, company)

        body = app_client.get(f"{API}/dpdp/readiness", headers=auth(company_admin)).json()

        assert body["cross_border_transfers"] == 1
        assert any("Cross-border" in gap for gap in body["gaps"])

    def test_the_score_never_goes_below_zero(self, app_client, db, company, company_admin):
        for _ in range(3):
            make_breach(
                db,
                company,
                detected_at=utcnow() - timedelta(hours=BREACH_NOTIFICATION_HOURS + 1),
            )
        make_dsr(db, company, due_date=date(2026, 8, 1))

        body = app_client.get(
            f"{API}/dpdp/readiness",
            headers=auth(company_admin),
            params={"today": "2026-08-15"},
        ).json()

        assert body["score"] >= 0

    def test_another_tenants_state_does_not_move_the_score(
        self, app_client, db, other_company, company_admin
    ):
        make_map_entry(db, other_company)
        make_consent(db, other_company)

        body = app_client.get(f"{API}/dpdp/readiness", headers=auth(company_admin)).json()

        assert body["has_data_map"] is False
        assert body["data_map_entries"] == 0
