"""The tamper-evident audit chain.

The point of these tests is not that ``record`` inserts a row. It is that an
edit to a row already written is *detectable* — which is the only property the
chain exists to provide, and the one that a refactor could silently remove
while every other test kept passing.
"""
from __future__ import annotations

import pytest

from app.core.tenancy import TenantContext
from app.models.audit import GENESIS_CHECKSUM, AuditTrail
from app.models.enums import AuditAction, UserRole
from app.services import audit as audit_service
from tests.conftest import make_user


def _ctx(user, org_id=None, home_org_id=None, delegated=False):
    return TenantContext(
        user=user,
        org_id=org_id or user.organization_id,
        home_org_id=home_org_id or user.organization_id,
        role=user.role,
        is_delegated=delegated,
    )


def _write(db, org_id, n=3, **kwargs):
    return [
        audit_service.record(
            db,
            organization_id=org_id,
            action=AuditAction.CREATE,
            entity_type="filing",
            entity_id=i,
            summary=f"Entry {i}",
            **kwargs,
        )
        for i in range(1, n + 1)
    ]


class TestChainConstruction:
    def test_first_entry_links_to_the_genesis_checksum(self, db, company):
        entry = audit_service.record(
            db,
            organization_id=company.id,
            action=AuditAction.LOGIN,
            entity_type="user",
        )
        assert entry.sequence == 1
        assert entry.prev_checksum == GENESIS_CHECKSUM

    def test_sequences_are_dense_and_start_at_one(self, db, company):
        entries = _write(db, company.id, 5)
        assert [e.sequence for e in entries] == [1, 2, 3, 4, 5]

    def test_each_entry_links_to_the_one_before(self, db, company):
        entries = _write(db, company.id, 4)
        # ``strict=False`` is the point, not an oversight: pairing a list with
        # its own tail is uneven by construction, so strict would raise on
        # every run regardless of the code under test.
        for previous, current in zip(entries, entries[1:], strict=False):
            assert current.prev_checksum == previous.checksum

    def test_sequences_are_per_organization(self, db, company, other_company):
        """One tenant's activity must not advance another's sequence."""
        _write(db, company.id, 3)
        theirs = _write(db, other_company.id, 2)
        assert [e.sequence for e in theirs] == [1, 2]

    def test_identical_content_in_two_chains_hashes_differently(
        self, db, company, other_company
    ):
        """The organization id is an input, so chains cannot be spliced."""
        ours = audit_service.record(
            db, organization_id=company.id, action=AuditAction.CREATE, entity_type="x"
        )
        theirs = audit_service.record(
            db,
            organization_id=other_company.id,
            action=AuditAction.CREATE,
            entity_type="x",
        )
        assert ours.checksum != theirs.checksum


class TestTamperDetection:
    def test_an_untouched_chain_verifies(self, db, company):
        _write(db, company.id, 6)
        db.commit()

        result = audit_service.verify_chain(db, company.id)
        assert result.is_valid
        assert result.entries_checked == 6
        assert result.broken_at_sequence is None

    def test_editing_an_entrys_summary_is_detected(self, db, company):
        _write(db, company.id, 5)
        db.commit()

        target = db.query(AuditTrail).filter_by(sequence=3).one()
        target.summary = "Nothing to see here"
        db.commit()

        result = audit_service.verify_chain(db, company.id)
        assert not result.is_valid
        assert result.broken_at_sequence == 3
        assert "checksum" in (result.reason or "").lower()

    def test_rewriting_the_actor_is_detected(self, db, company, company_admin):
        """The question the trail exists to answer is *who*."""
        _write(db, company.id, 3, user_id=company_admin.id, actor_label="Priya Sharma")
        db.commit()

        target = db.query(AuditTrail).filter_by(sequence=2).one()
        target.actor_label = "Somebody Else"
        db.commit()

        result = audit_service.verify_chain(db, company.id)
        assert not result.is_valid
        assert result.broken_at_sequence == 2

    def test_rewriting_the_ip_address_is_detected(self, db, company):
        _write(db, company.id, 2, ip_address="203.0.113.9")
        db.commit()

        target = db.query(AuditTrail).filter_by(sequence=1).one()
        target.ip_address = "198.51.100.4"
        db.commit()

        assert not audit_service.verify_chain(db, company.id).is_valid

    def test_altering_the_before_after_payload_is_detected(self, db, company):
        audit_service.record(
            db,
            organization_id=company.id,
            action=AuditAction.UPDATE,
            entity_type="filing",
            before={"tax_payable_paise": 500000},
            after={"tax_payable_paise": 100000},
        )
        db.commit()

        target = db.query(AuditTrail).filter_by(sequence=1).one()
        target.after_json = {"tax_payable_paise": 500000}
        db.commit()

        assert not audit_service.verify_chain(db, company.id).is_valid

    def test_deleting_a_middle_entry_leaves_a_sequence_gap(self, db, company):
        """Detected as a gap, before the checksum check even runs."""
        _write(db, company.id, 4)
        db.commit()

        db.query(AuditTrail).filter_by(sequence=2).delete()
        db.commit()

        result = audit_service.verify_chain(db, company.id)
        assert not result.is_valid
        assert result.broken_at_sequence == 3
        assert "gap" in (result.reason or "").lower()

    def test_verification_stops_at_the_first_break(self, db, company):
        """Everything after a break chains off a wrong value and is unverifiable."""
        _write(db, company.id, 10)
        db.commit()

        target = db.query(AuditTrail).filter_by(sequence=2).one()
        target.summary = "tampered"
        db.commit()

        result = audit_service.verify_chain(db, company.id)
        assert result.broken_at_sequence == 2
        # Reported one failure, not nine.
        assert result.entries_checked == 2

    def test_verification_batches_without_changing_the_answer(self, db, company):
        _write(db, company.id, 12)
        db.commit()
        assert audit_service.verify_chain(db, company.id, batch_size=5).is_valid

    def test_an_empty_chain_is_valid(self, db, company):
        result = audit_service.verify_chain(db, company.id)
        assert result.is_valid
        assert result.entries_checked == 0


class TestRedaction:
    def test_secret_keys_are_stripped_from_the_payload(self, db, company):
        entry = audit_service.record(
            db,
            organization_id=company.id,
            action=AuditAction.UPDATE,
            entity_type="user",
            after={
                "email": "someone@example.test",
                "password_hash": "$2b$12$realhash",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            },
        )
        assert entry.after_json["email"] == "someone@example.test"
        assert entry.after_json["password_hash"] == "[redacted]"
        assert entry.after_json["totp_secret"] == "[redacted]"

    def test_redaction_reaches_into_nested_structures(self, db, company):
        entry = audit_service.record(
            db,
            organization_id=company.id,
            action=AuditAction.UPDATE,
            entity_type="settings",
            after={"integrations": [{"name": "wa", "whatsapp_access_token": "tok"}]},
        )
        assert entry.after_json["integrations"][0]["whatsapp_access_token"] == "[redacted]"

    def test_redaction_is_case_insensitive(self, db, company):
        entry = audit_service.record(
            db,
            organization_id=company.id,
            action=AuditAction.UPDATE,
            entity_type="user",
            after={"Password": "hunter2", "API_KEY": "sk-live"},
        )
        assert entry.after_json["Password"] == "[redacted]"
        assert entry.after_json["API_KEY"] == "[redacted]"

    def test_a_redacted_chain_still_verifies(self, db, company):
        """Redaction happens before hashing, so the stored payload is what was hashed."""
        audit_service.record(
            db,
            organization_id=company.id,
            action=AuditAction.UPDATE,
            entity_type="user",
            after={"password": "hunter2"},
        )
        db.commit()
        assert audit_service.verify_chain(db, company.id).is_valid


class TestDelegatedRecording:
    def test_a_delegated_action_lands_in_both_chains(self, db, ca_firm, company):
        """The client's history shows what happened; the firm's shows who did it."""
        firm_user = make_user(db, ca_firm, role=UserRole.COMPLIANCE_MANAGER)
        ctx = _ctx(firm_user, org_id=company.id, home_org_id=ca_firm.id, delegated=True)

        entries = audit_service.record_for(
            db,
            ctx,
            action=AuditAction.SUBMIT,
            entity_type="filing",
            entity_id=42,
            summary="GSTR-3B for 2026-07 submitted",
        )
        db.commit()

        assert len(entries) == 2
        assert {e.organization_id for e in entries} == {company.id, ca_firm.id}

        firm_entry = next(e for e in entries if e.organization_id == ca_firm.id)
        assert f"for client organization {company.id}" in firm_entry.summary

        assert audit_service.verify_chain(db, company.id).is_valid
        assert audit_service.verify_chain(db, ca_firm.id).is_valid

    def test_a_non_delegated_action_writes_one_entry(self, db, company, company_admin):
        entries = audit_service.record_for(
            db, _ctx(company_admin), action=AuditAction.CREATE, entity_type="filing"
        )
        assert len(entries) == 1
        assert entries[0].organization_id == company.id

    def test_the_actor_is_named_in_both_entries(self, db, ca_firm, company):
        firm_user = make_user(db, ca_firm, full_name="CA Meera Sharma")
        ctx = _ctx(firm_user, org_id=company.id, home_org_id=ca_firm.id, delegated=True)

        entries = audit_service.record_for(
            db, ctx, action=AuditAction.APPROVE, entity_type="filing"
        )
        assert all(e.actor_label == "CA Meera Sharma" for e in entries)
        assert all(e.user_id == firm_user.id for e in entries)


class TestChainHead:
    def test_head_reports_the_tip(self, db, company):
        _write(db, company.id, 4)
        db.commit()

        head = audit_service.chain_head(db, company.id)
        assert head["sequence"] == 4
        assert head["entry_count"] == 4

        last = db.query(AuditTrail).filter_by(sequence=4).one()
        assert head["checksum"] == last.checksum

    def test_head_of_an_empty_chain_is_the_genesis_value(self, db, company):
        head = audit_service.chain_head(db, company.id)
        assert head["sequence"] == 0
        assert head["checksum"] == GENESIS_CHECKSUM
        assert head["entry_count"] == 0

    def test_head_matches_verification(self, db, company):
        _write(db, company.id, 3)
        db.commit()
        assert (
            audit_service.chain_head(db, company.id)["checksum"]
            == audit_service.verify_chain(db, company.id).head_checksum
        )


class TestChecksumInputs:
    """Every field a reader relies on must be an input to the hash."""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("user_id", 99),
            ("actor_label", "Someone Else"),
            ("entity_type", "organization"),
            ("entity_id", "999"),
            ("ip_address", "10.0.0.1"),
            ("summary", "different"),
        ],
    )
    def test_changing_any_recorded_field_changes_the_checksum(self, field, value):
        from datetime import UTC, datetime

        base = {
            "organization_id": 1,
            "sequence": 1,
            "user_id": 5,
            "actor_label": "Priya Sharma",
            "action": "create",
            "entity_type": "filing",
            "entity_id": "42",
            "timestamp": datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
            "ip_address": "203.0.113.9",
            "before": None,
            "after": {"status": "draft"},
            "summary": "created",
            "prev_checksum": GENESIS_CHECKSUM,
        }
        original = audit_service.compute_checksum(**base)
        assert audit_service.compute_checksum(**{**base, field: value}) != original

    def test_payload_key_order_does_not_change_the_checksum(self):
        """A caller that built the dict differently must not fail verification."""
        from datetime import UTC, datetime

        base = {
            "organization_id": 1,
            "sequence": 1,
            "user_id": None,
            "actor_label": "system",
            "action": "update",
            "entity_type": "filing",
            "entity_id": "1",
            "timestamp": datetime(2026, 8, 8, tzinfo=UTC),
            "ip_address": None,
            "before": None,
            "summary": None,
            "prev_checksum": GENESIS_CHECKSUM,
        }
        first = audit_service.compute_checksum(**base, after={"a": 1, "b": 2})
        second = audit_service.compute_checksum(**base, after={"b": 2, "a": 1})
        assert first == second

    def test_field_boundaries_cannot_be_shifted(self):
        """``("ab","c")`` and ``("a","bc")`` must not hash alike."""
        from datetime import UTC, datetime

        base = {
            "organization_id": 1,
            "sequence": 1,
            "user_id": None,
            "actor_label": "system",
            "action": "create",
            "timestamp": datetime(2026, 8, 8, tzinfo=UTC),
            "ip_address": None,
            "before": None,
            "after": None,
            "summary": None,
            "prev_checksum": GENESIS_CHECKSUM,
        }
        first = audit_service.compute_checksum(
            **base, entity_type="filing", entity_id="1"
        )
        second = audit_service.compute_checksum(
            **base, entity_type="filin", entity_id="g1"
        )
        assert first != second
