"""The properties everything else is built on: enums, crypto, soft delete, tenancy.

These are the tests that would have caught the two bugs fixed in commit 5f1af76 —
the ``StrEnum`` stringification that opened the whole RBAC layer, and the
soft-delete uniqueness constraint that admitted duplicate GSTINs. Both are
asserted directly here rather than trusted to a base class or a database
default.
"""
from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy.exc import IntegrityError

from app.core.crypto import decrypt, encrypt, fingerprint, is_encrypted
from app.models.enums import (
    PRIVILEGED_ROLES,
    AuditAction,
    FilingStatus,
    OrgType,
    Regulation,
    UserRole,
    role_rank,
)
from app.models.organization import Organization
from tests.conftest import make_org, make_user


class TestEnums:
    """``str(member)`` must be the value. Three things break silently if not."""

    def test_str_yields_the_value_not_the_class_name(self):
        # The bug: a plain ``(str, Enum)`` returns "UserRole.ADMIN" here under
        # Python 3.11. That string is hashed into every audit checksum, looked
        # up in the role table, and carried as a JWT claim.
        assert str(UserRole.ADMIN) == "admin"
        assert str(FilingStatus.IN_REVIEW) == "in_review"
        assert str(AuditAction.SOFT_DELETE) == "soft_delete"
        assert str(Regulation.INCOME_TAX) == "income_tax"
        assert str(OrgType.CA_FIRM) == "ca_firm"

    def test_members_compare_equal_to_their_stored_string(self):
        assert UserRole.STAFF == "staff"
        assert FilingStatus.DRAFT == "draft"

    def test_f_string_interpolation_is_the_value(self):
        # Distinct from ``str()``: an f-string goes through ``__format__``,
        # which is a different method and was correct even when ``__str__``
        # was not. Both are used in this codebase.
        assert f"{UserRole.COMPLIANCE_MANAGER}" == "compliance_manager"

    def test_role_rank_orders_the_four_roles(self):
        assert (
            role_rank(UserRole.ADMIN)
            > role_rank(UserRole.COMPLIANCE_MANAGER)
            > role_rank(UserRole.STAFF)
            > role_rank(UserRole.READ_ONLY)
        )

    def test_role_rank_accepts_the_string_form(self):
        # This is the path that was broken: ``role_rank(str(role))`` returned
        # -1 for every role, so "at least" comparisons answered True for
        # every pair and RBAC was open.
        for role in UserRole:
            assert role_rank(str(role)) == role_rank(role)
            assert role_rank(str(role)) >= 0

    def test_unknown_role_ranks_below_everything(self):
        """An unrecognised role must deny, not raise and not permit."""
        assert role_rank("superuser") == -1
        assert role_rank("") == -1
        assert role_rank("superuser") < role_rank(UserRole.READ_ONLY)

    def test_privileged_roles_are_the_two_that_need_totp(self):
        # noqa SIM300: ruff reads the uppercase name as the constant and wants
        # the operands swapped. Here the name is the subject under test and the
        # literal is the expectation, so this order is the readable one.
        assert PRIVILEGED_ROLES == {UserRole.ADMIN, UserRole.COMPLIANCE_MANAGER}  # noqa: SIM300


class TestColumnEncryption:
    def test_round_trip(self):
        assert decrypt(encrypt("27AABCU9603R1ZM")) == "27AABCU9603R1ZM"

    def test_ciphertext_differs_on_every_write(self):
        """A fresh nonce per write, so the column leaks not even equality."""
        first, second = encrypt("ABCDE1234F"), encrypt("ABCDE1234F")
        assert first != second
        assert decrypt(first) == decrypt(second) == "ABCDE1234F"

    def test_none_and_empty_pass_through(self):
        # An encrypted empty string is still a ciphertext, and a column
        # holding one reads as "there is a PAN here" to a presence check.
        assert encrypt(None) is None
        assert encrypt("") == ""
        assert decrypt(None) is None

    def test_plaintext_without_the_marker_is_returned_as_is(self):
        """Rows imported before encryption was switched on stay readable."""
        assert decrypt("27AABCU9603R1ZM") == "27AABCU9603R1ZM"
        assert not is_encrypted("27AABCU9603R1ZM")

    def test_tampered_ciphertext_raises_rather_than_returning_garbage(self):
        stored = encrypt("ABCDE1234F")
        assert stored is not None
        tampered = stored[:-4] + "AAAA"
        # The specific exception, not a blind ``Exception``: AES-GCM refusing
        # the tag is the assertion. A bare ``Exception`` would pass just as
        # happily on a TypeError from a decrypt that had stopped working
        # altogether, which is the opposite of what this test claims.
        with pytest.raises(InvalidTag):
            decrypt(tampered)

    def test_fingerprint_is_deterministic_and_normalising(self):
        """``27aabcu9603r1zm`` and ``27AABCU9603R1ZM `` are one GSTIN."""
        assert fingerprint("27AABCU9603R1ZM") == fingerprint(" 27aabcu9603r1zm ")
        assert fingerprint("27AABCU9603R1ZM") != fingerprint("27AABCU9603R1ZN")

    def test_fingerprint_is_not_the_ciphertext(self):
        value = "ABCDE1234F"
        assert fingerprint(value) != encrypt(value)
        # And it is not reversible — a keyed hash, not a cipher.
        assert value not in (fingerprint(value) or "")


class TestSoftDeleteUniqueness:
    """The partial unique index, which a composite constraint could not express."""

    def test_two_live_organizations_cannot_share_a_gstin(self, db):
        make_org(db, gstin="27AABCU9603R1ZM")
        db.commit()

        # The factory flushes, so the index fires there rather than at commit.
        with pytest.raises(IntegrityError):
            make_org(db, name="Impostor Pvt Ltd", gstin="27AABCU9603R1ZM")
        db.rollback()

    def test_soft_deleting_frees_the_gstin_for_re_onboarding(self, db):
        """The case a plain UniqueConstraint gets wrong in the other direction."""
        first = make_org(db, gstin="27AABCU9603R1ZM")
        db.commit()

        first.soft_delete()
        db.commit()

        second = make_org(db, name="Reborn Pvt Ltd", gstin="27AABCU9603R1ZM")
        db.commit()
        assert second.id != first.id

    def test_two_soft_deleted_rows_may_share_a_gstin(self, db):
        """The bug the composite constraint had: it only fired between deleted rows."""
        first = make_org(db, gstin="27AABCU9603R1ZM")
        first.soft_delete()
        db.commit()

        second = make_org(db, name="Second Pvt Ltd", gstin="27AABCU9603R1ZM")
        second.soft_delete()
        db.commit()

        assert first.id != second.id

    def test_email_is_unique_per_organization_not_globally(self, db, company, other_company):
        """One accountant, an account at their firm and at a client's company."""
        make_user(db, company, email="ca@example.test")
        make_user(db, other_company, email="ca@example.test")
        db.commit()

        with pytest.raises(IntegrityError):
            make_user(db, company, email="ca@example.test", full_name="Duplicate")
        db.rollback()

    def test_soft_delete_sets_a_timestamp_rather_than_removing_the_row(self, db, company):
        company.soft_delete()
        db.commit()

        assert company.is_deleted
        assert db.get(Organization, company.id) is not None


class TestTenancyHelpers:
    def test_scoped_filters_to_one_tenant_and_to_live_rows(self, db, company, other_company):
        from app.core.tenancy import TenantContext, scoped

        gone = make_org(db, name="Deleted Client")
        db.commit()

        user = make_user(db, company)
        ctx = TenantContext(
            user=user, org_id=company.id, home_org_id=company.id, role=UserRole.ADMIN
        )

        # Organization has no organization_id column, so scope a tenant table.
        from app.models.document import Document

        for org, title in (
            (company, "Ours"),
            (other_company, "Theirs"),
            (gone, "Third party"),
        ):
            db.add(
                Document(
                    organization_id=org.id,
                    title=title,
                    storage_path=f"/tmp/{title}",
                )
            )
        db.flush()

        visible = db.execute(scoped(Document, ctx)).scalars().all()
        assert [d.title for d in visible] == ["Ours"]

    def test_scoped_hides_soft_deleted_rows_unless_asked(self, db, company):
        from app.core.tenancy import TenantContext, scoped
        from app.models.document import Document

        user = make_user(db, company)
        ctx = TenantContext(
            user=user, org_id=company.id, home_org_id=company.id, role=UserRole.ADMIN
        )

        doc = Document(organization_id=company.id, title="Notice", storage_path="/tmp/n")
        db.add(doc)
        db.flush()
        doc.soft_delete()
        db.flush()

        assert db.execute(scoped(Document, ctx)).scalars().all() == []
        assert (
            len(db.execute(scoped(Document, ctx, include_deleted=True)).scalars().all())
            == 1
        )

    def test_catalogue_scoped_returns_system_rows_plus_your_own(
        self, db, company, other_company
    ):
        from app.core.tenancy import TenantContext, catalogue_scoped
        from app.models.enums import Frequency
        from app.models.obligation import ComplianceObligation

        user = make_user(db, company)
        ctx = TenantContext(
            user=user, org_id=company.id, home_org_id=company.id, role=UserRole.ADMIN
        )

        for org_id, code, is_system in (
            (None, "system.one", True),
            (company.id, "ours.one", False),
            (other_company.id, "theirs.one", False),
        ):
            db.add(
                ComplianceObligation(
                    organization_id=org_id,
                    code=code,
                    title=code,
                    regulation=Regulation.GST,
                    frequency=Frequency.MONTHLY,
                    due_day=20,
                    is_system=is_system,
                )
            )
        db.flush()

        codes = {
            o.code
            for o in db.execute(catalogue_scoped(ComplianceObligation, ctx)).scalars()
        }
        assert codes == {"system.one", "ours.one"}
