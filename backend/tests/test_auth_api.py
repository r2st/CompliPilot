"""The authentication surface: registration, login, TOTP, context switching, users.

These are the endpoints an attacker reaches without credentials, so the tests
lean on the refusals rather than the happy paths. Three properties get the most
attention because getting any of them wrong is not a bug that shows up in
normal use:

* a failed login says the same thing whether or not the account exists, since
  distinguishing the two publishes a list of who banks with which CA firm;
* the lockout counter actually locks, and actually clears;
* a token minted for one organization does not authenticate against another.

The user-administration endpoints are tested through the *home* organization
rather than the acting one. A CA firm member inside a client's context is still
asking about their own colleagues, and that distinction is invisible until
someone writes the test that switches context first.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp
import pytest

from app.core.security import (
    create_access_token,
    create_refresh_token,
    generate_totp_secret,
    store_totp_secret,
)
from app.models.enums import AuditAction, EntityType, OrgType, UserRole
from app.models.organization import Organization
from app.models.user import User
from tests.conftest import (
    API,
    auth,
    make_assignment,
    make_engagement,
    make_org,
    make_user,
)


def _login(client, email: str, password: str, **extra):
    return client.post(f"{API}/auth/login", json={"email": email, "password": password, **extra})


def _enrol_totp(db, user: User) -> str:
    """Give *user* a confirmed second factor and return the raw secret."""
    secret = generate_totp_secret()
    user.totp_secret = store_totp_secret(secret)
    user.totp_confirmed_at = datetime.now(UTC)
    db.flush()
    return secret


class TestRegistration:
    def test_creates_the_organization_and_its_first_admin(self, app_client, db):
        response = app_client.post(
            f"{API}/auth/register",
            json={
                "organization_name": "Nandi Foods Pvt Ltd",
                "organization_type": "company",
                "entity_type": "private_limited",
                "state": "Karnataka",
                "full_name": "Deepa Iyer",
                "email": "deepa@nandifoods.example.com",
                "password": "a-long-enough-passphrase",
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["user"]["role"] == "admin"
        assert body["access_token"] and body["refresh_token"]

        org = db.query(Organization).filter_by(name="Nandi Foods Pvt Ltd").one()
        assert org.type == OrgType.COMPANY
        assert org.entity_type == EntityType.PRIVATE_LIMITED

    def test_the_first_admin_can_immediately_use_the_token_it_returns(self, app_client):
        token = app_client.post(
            f"{API}/auth/register",
            json={
                "organization_name": "Nandi Foods Pvt Ltd",
                "full_name": "Deepa Iyer",
                "email": "deepa@nandifoods.example.com",
                "password": "a-long-enough-passphrase",
            },
        ).json()["access_token"]

        me = app_client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert me.status_code == 200
        assert me.json()["organization_name"] == "Nandi Foods Pvt Ltd"

    def test_the_email_is_stored_lowercased(self, app_client, db):
        app_client.post(
            f"{API}/auth/register",
            json={
                "organization_name": "Nandi Foods Pvt Ltd",
                "full_name": "Deepa Iyer",
                "email": "Deepa@NandiFoods.Example.com",
                "password": "a-long-enough-passphrase",
            },
        )

        assert db.query(User).one().email == "deepa@nandifoods.example.com"

    def test_registration_is_audited(self, app_client, db):
        from app.models.audit import AuditTrail

        app_client.post(
            f"{API}/auth/register",
            json={
                "organization_name": "Nandi Foods Pvt Ltd",
                "full_name": "Deepa Iyer",
                "email": "deepa@nandifoods.example.com",
                "password": "a-long-enough-passphrase",
            },
        )

        entry = db.query(AuditTrail).filter_by(action=AuditAction.CREATE).one()
        assert entry.entity_type == "organization"

    def test_a_short_password_is_refused_with_the_shared_envelope(self, app_client):
        response = app_client.post(
            f"{API}/auth/register",
            json={
                "organization_name": "Nandi Foods Pvt Ltd",
                "full_name": "Deepa Iyer",
                "email": "deepa@nandifoods.example.com",
                "password": "short",
            },
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
        assert response.json()["error"]["details"]["fields"]


class TestLogin:
    def test_correct_credentials_return_a_token_pair(self, app_client, company_admin):
        response = _login(app_client, company_admin.email, "correct-horse-battery")

        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["user"]["id"] == company_admin.id
        assert "totp_required" not in body

    def test_email_matching_is_case_insensitive(self, app_client, company_admin):
        response = _login(app_client, company_admin.email.upper(), "correct-horse-battery")

        assert response.status_code == 200

    def test_the_last_login_timestamp_is_recorded(self, app_client, db, company_admin):
        assert company_admin.last_login_at is None

        _login(app_client, company_admin.email, "correct-horse-battery")

        db.refresh(company_admin)
        assert company_admin.last_login_at is not None

    def test_a_wrong_password_and_an_unknown_address_are_indistinguishable(
        self, app_client, company_admin
    ):
        """The one property that stops the login form being a directory."""
        wrong = _login(app_client, company_admin.email, "not-the-password")
        unknown = _login(app_client, "nobody@example.com", "not-the-password")

        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()

    def test_a_deactivated_account_is_refused(self, app_client, db, company_admin):
        company_admin.is_active = False
        db.flush()

        response = _login(app_client, company_admin.email, "correct-horse-battery")

        assert response.status_code == 401
        assert "deactivated" in response.json()["error"]["message"]

    def test_a_soft_deleted_user_cannot_log_in(self, app_client, db, company_admin):
        company_admin.soft_delete()
        db.flush()

        response = _login(app_client, company_admin.email, "correct-horse-battery")

        assert response.status_code == 401

    def test_a_failed_login_is_audited(self, app_client, db, company_admin):
        from app.models.audit import AuditTrail

        _login(app_client, company_admin.email, "not-the-password")

        entry = db.query(AuditTrail).filter_by(action=AuditAction.LOGIN_FAILED).one()
        assert entry.entity_id == str(company_admin.id)

    def test_a_failure_against_an_unknown_address_writes_no_audit_entry(
        self, app_client, db
    ):
        """There is no organization to file it under, and inventing one would
        put an attacker's typos in a real tenant's history."""
        from app.models.audit import AuditTrail

        _login(app_client, "nobody@example.com", "whatever")

        assert db.query(AuditTrail).count() == 0


class TestLockout:
    def test_five_failures_lock_the_account(self, app_client, db, company_admin):
        for _ in range(5):
            _login(app_client, company_admin.email, "wrong")

        db.refresh(company_admin)
        assert company_admin.locked_until is not None

        # And the correct password no longer works while the window is open.
        response = _login(app_client, company_admin.email, "correct-horse-battery")
        assert response.status_code == 401
        assert "locked" in response.json()["error"]["message"]

    def test_four_failures_do_not(self, app_client, db, company_admin):
        for _ in range(4):
            _login(app_client, company_admin.email, "wrong")

        db.refresh(company_admin)
        assert company_admin.locked_until is None
        assert _login(app_client, company_admin.email, "correct-horse-battery").status_code == 200

    def test_a_success_resets_the_counter(self, app_client, db, company_admin):
        for _ in range(3):
            _login(app_client, company_admin.email, "wrong")

        _login(app_client, company_admin.email, "correct-horse-battery")

        db.refresh(company_admin)
        assert company_admin.failed_login_count == 0

    def test_an_expired_lock_clears_rather_than_re_locking_on_the_next_slip(
        self, app_client, db, company_admin
    ):
        """The counter has to be cleared with the lock.

        Leaving it at five means the first typo after the window expires locks
        the account again immediately, which reads to the user as a permanent
        lockout.
        """
        company_admin.locked_until = datetime.now(UTC) - timedelta(minutes=1)
        company_admin.failed_login_count = 5
        db.flush()

        assert _login(app_client, company_admin.email, "correct-horse-battery").status_code == 200

        db.refresh(company_admin)
        assert company_admin.locked_until is None
        assert company_admin.failed_login_count == 0


class TestTotpLogin:
    def test_an_enrolled_user_gets_a_challenge_rather_than_tokens(
        self, app_client, db, company_admin
    ):
        _enrol_totp(db, company_admin)

        response = _login(app_client, company_admin.email, "correct-horse-battery")

        assert response.status_code == 200
        body = response.json()
        assert body["totp_required"] is True
        assert body["challenge_token"]
        assert "access_token" not in body

    def test_the_code_can_be_sent_with_the_password_in_one_call(
        self, app_client, db, company_admin
    ):
        secret = _enrol_totp(db, company_admin)

        response = _login(
            app_client,
            company_admin.email,
            "correct-horse-battery",
            totp_code=pyotp.TOTP(secret).now(),
        )

        assert response.status_code == 200
        assert response.json()["access_token"]

    def test_a_wrong_code_fails_and_counts_towards_the_lockout(
        self, app_client, db, company_admin
    ):
        _enrol_totp(db, company_admin)

        response = _login(
            app_client, company_admin.email, "correct-horse-battery", totp_code="000000"
        )

        assert response.status_code == 401
        db.refresh(company_admin)
        assert company_admin.failed_login_count == 1

    def test_a_privileged_role_is_challenged_before_enrolling_when_totp_is_mandated(
        self, app_client, db, company_admin, monkeypatch
    ):
        """Refusing outright would strand the only Admin of a new organization."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "require_totp_for_privileged_roles", True)

        response = _login(app_client, company_admin.email, "correct-horse-battery")

        assert response.json()["totp_required"] is True

    def test_an_unprivileged_role_is_not_challenged_by_the_mandate(
        self, app_client, company_staff, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "require_totp_for_privileged_roles", True)

        response = _login(app_client, company_staff.email, "correct-horse-battery")

        assert response.json()["access_token"]


class TestTotpEnrolment:
    def test_setup_then_confirm_turns_the_second_factor_on(
        self, app_client, db, company_admin
    ):
        headers = auth(company_admin)

        setup = app_client.post(f"{API}/auth/totp/setup", headers=headers)
        assert setup.status_code == 200
        secret = setup.json()["secret"]
        assert setup.json()["confirmed"] is False
        assert "otpauth://" in setup.json()["provisioning_uri"]

        # Not yet active: the secret exists but was never proved to work.
        db.refresh(company_admin)
        assert not company_admin.has_totp

        confirm = app_client.post(
            f"{API}/auth/totp/confirm",
            headers=headers,
            json={"code": pyotp.TOTP(secret).now()},
        )

        assert confirm.status_code == 200
        db.refresh(company_admin)
        assert company_admin.has_totp

    def test_confirming_with_a_wrong_code_leaves_it_off(self, app_client, db, company_admin):
        headers = auth(company_admin)
        app_client.post(f"{API}/auth/totp/setup", headers=headers)

        response = app_client.post(
            f"{API}/auth/totp/confirm", headers=headers, json={"code": "000000"}
        )

        assert response.status_code == 401
        db.refresh(company_admin)
        assert not company_admin.has_totp

    def test_confirming_without_setting_up_first_is_a_conflict(self, app_client, company_admin):
        response = app_client.post(
            f"{API}/auth/totp/confirm", headers=auth(company_admin), json={"code": "123456"}
        )

        assert response.status_code == 409

    def test_re_enrolling_over_a_confirmed_factor_is_refused(
        self, app_client, db, company_admin
    ):
        """Otherwise rotating the secret is a way to strip the second factor."""
        _enrol_totp(db, company_admin)

        response = app_client.post(f"{API}/auth/totp/setup", headers=auth(company_admin))

        assert response.status_code == 409

    def test_disabling_requires_the_current_code(self, app_client, db, company_admin):
        secret = _enrol_totp(db, company_admin)

        assert (
            app_client.post(
                f"{API}/auth/totp/disable",
                headers=auth(company_admin),
                json={"code": "000000"},
            ).status_code
            == 401
        )

        response = app_client.post(
            f"{API}/auth/totp/disable",
            headers=auth(company_admin),
            json={"code": pyotp.TOTP(secret).now()},
        )

        assert response.status_code == 200
        db.refresh(company_admin)
        assert not company_admin.has_totp

    def test_a_privileged_role_may_not_disable_it_where_it_is_mandated(
        self, app_client, db, company_admin, monkeypatch
    ):
        from app.core.config import settings

        secret = _enrol_totp(db, company_admin)
        monkeypatch.setattr(settings, "require_totp_for_privileged_roles", True)

        response = app_client.post(
            f"{API}/auth/totp/disable",
            headers=auth(company_admin),
            json={"code": pyotp.TOTP(secret).now()},
        )

        assert response.status_code == 403
        db.refresh(company_admin)
        assert company_admin.has_totp


class TestTokenHandling:
    def test_a_request_with_no_token_is_401_in_the_shared_envelope(self, app_client):
        response = app_client.get(f"{API}/auth/me")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"

    def test_a_garbage_token_is_401(self, app_client):
        response = app_client.get(
            f"{API}/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
        )

        assert response.status_code == 401

    def test_a_refresh_token_does_not_work_as_an_access_token(self, app_client, company_admin):
        refresh = create_refresh_token(company_admin.id, org_id=company_admin.organization_id)

        response = app_client.get(
            f"{API}/auth/me", headers={"Authorization": f"Bearer {refresh}"}
        )

        assert response.status_code == 401

    def test_a_token_whose_org_claim_no_longer_matches_the_user_is_rejected(
        self, app_client, company_admin, other_company
    ):
        """A user moved between organizations must not keep the old token.

        Otherwise a transfer silently re-points an outstanding token at data
        the holder never had.
        """
        stale = create_access_token(
            company_admin.id, org_id=other_company.id, role=str(company_admin.role)
        )

        response = app_client.get(
            f"{API}/auth/me", headers={"Authorization": f"Bearer {stale}"}
        )

        assert response.status_code == 401

    def test_refresh_mints_a_new_access_token(self, app_client, company_admin):
        refresh = create_refresh_token(company_admin.id, org_id=company_admin.organization_id)

        response = app_client.post(f"{API}/auth/refresh", json={"refresh_token": refresh})

        assert response.status_code == 200
        assert response.json()["user"]["id"] == company_admin.id

    def test_refresh_reads_the_role_from_the_database_not_the_token(
        self, app_client, db, company_admin
    ):
        """Which is what makes a demotion take effect at the next refresh."""
        refresh = create_refresh_token(company_admin.id, org_id=company_admin.organization_id)
        company_admin.role = UserRole.READ_ONLY
        db.flush()

        response = app_client.post(f"{API}/auth/refresh", json={"refresh_token": refresh})

        assert response.json()["user"]["role"] == "read_only"

    def test_refresh_is_refused_for_a_deactivated_user(self, app_client, db, company_admin):
        refresh = create_refresh_token(company_admin.id, org_id=company_admin.organization_id)
        company_admin.is_active = False
        db.flush()

        assert (
            app_client.post(
                f"{API}/auth/refresh", json={"refresh_token": refresh}
            ).status_code
            == 401
        )

    def test_an_access_token_is_not_accepted_for_refresh(self, app_client, company_admin):
        access = create_access_token(
            company_admin.id,
            org_id=company_admin.organization_id,
            role=str(company_admin.role),
        )

        response = app_client.post(f"{API}/auth/refresh", json={"refresh_token": access})

        assert response.status_code == 401

    def test_the_oauth_form_endpoint_works_for_the_docs_authorize_button(
        self, app_client, company_admin
    ):
        response = app_client.post(
            f"{API}/auth/token",
            data={"username": company_admin.email, "password": "correct-horse-battery"},
        )

        assert response.status_code == 200
        assert response.json()["access_token"]

    def test_the_form_endpoint_refuses_an_account_that_needs_a_second_factor(
        self, app_client, db, company_admin
    ):
        """The form flow has nowhere to put the code."""
        _enrol_totp(db, company_admin)

        response = app_client.post(
            f"{API}/auth/token",
            data={"username": company_admin.email, "password": "correct-horse-battery"},
        )

        assert response.status_code == 401


class TestSessionContext:
    def test_a_company_user_sees_their_own_organization_and_no_clients(
        self, app_client, company, company_admin
    ):
        response = app_client.get(f"{API}/auth/me", headers=auth(company_admin))

        body = response.json()
        assert body["organization_id"] == body["home_organization_id"] == company.id
        assert body["is_delegated"] is False
        assert body["available_clients"] == []

    def test_a_firm_admin_sees_every_client_of_the_firm(
        self, app_client, db, ca_firm, firm_admin, company, other_company
    ):
        make_engagement(db, ca_firm, company)
        make_engagement(db, ca_firm, other_company)

        response = app_client.get(f"{API}/auth/me", headers=auth(firm_admin))

        names = {c["name"] for c in response.json()["available_clients"]}
        assert names == {company.name, other_company.name}

    def test_firm_staff_see_only_the_clients_they_are_assigned(
        self, app_client, db, ca_firm, firm_staff, company, other_company
    ):
        assigned = make_engagement(db, ca_firm, company)
        make_engagement(db, ca_firm, other_company)
        make_assignment(db, firm_staff, assigned)

        response = app_client.get(f"{API}/auth/me", headers=auth(firm_staff))

        names = {c["name"] for c in response.json()["available_clients"]}
        assert names == {company.name}

    def test_acting_for_a_client_reports_the_client_as_the_organization(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        make_engagement(db, ca_firm, company)

        response = app_client.get(
            f"{API}/auth/me", headers=auth(firm_admin, client_org_id=company.id)
        )

        body = response.json()
        assert body["organization_id"] == company.id
        assert body["home_organization_id"] == ca_firm.id
        assert body["is_delegated"] is True


class TestSwitchClient:
    def test_a_firm_admin_receives_a_token_scoped_to_the_client(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        make_engagement(db, ca_firm, company)

        response = app_client.post(
            f"{API}/auth/switch-client",
            headers=auth(firm_admin),
            json={"client_org_id": company.id},
        )

        assert response.status_code == 200
        token = response.json()["access_token"]

        me = app_client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.json()["organization_id"] == company.id
        assert me.json()["is_delegated"] is True

    def test_switching_back_returns_a_token_for_the_firm(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        make_engagement(db, ca_firm, company)

        token = app_client.post(
            f"{API}/auth/switch-client",
            headers=auth(firm_admin, client_org_id=company.id),
            json={"client_org_id": None},
        ).json()["access_token"]

        me = app_client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.json()["organization_id"] == ca_firm.id
        assert me.json()["is_delegated"] is False

    def test_a_company_user_cannot_switch_into_anyone(
        self, app_client, company_admin, other_company
    ):
        response = app_client.post(
            f"{API}/auth/switch-client",
            headers=auth(company_admin),
            json={"client_org_id": other_company.id},
        )

        assert response.status_code == 403

    def test_a_firm_cannot_switch_into_a_company_it_has_no_engagement_with(
        self, app_client, firm_admin, other_company
    ):
        response = app_client.post(
            f"{API}/auth/switch-client",
            headers=auth(firm_admin),
            json={"client_org_id": other_company.id},
        )

        assert response.status_code == 403

    def test_a_terminated_engagement_is_not_switchable(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        engagement = make_engagement(db, ca_firm, company)
        engagement.status = "terminated"
        db.flush()

        response = app_client.post(
            f"{API}/auth/switch-client",
            headers=auth(firm_admin),
            json={"client_org_id": company.id},
        )

        assert response.status_code == 403

    def test_unassigned_staff_cannot_switch_into_a_client_of_their_own_firm(
        self, app_client, db, ca_firm, firm_staff, company
    ):
        make_engagement(db, ca_firm, company)

        response = app_client.post(
            f"{API}/auth/switch-client",
            headers=auth(firm_staff),
            json={"client_org_id": company.id},
        )

        assert response.status_code == 403

    def test_the_switch_is_recorded_in_the_clients_own_history(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        from app.models.audit import AuditTrail

        make_engagement(db, ca_firm, company)
        app_client.post(
            f"{API}/auth/switch-client",
            headers=auth(firm_admin),
            json={"client_org_id": company.id},
        )

        entry = db.query(AuditTrail).filter_by(action=AuditAction.READ).one()
        assert entry.organization_id == company.id
        assert ca_firm.name in entry.summary

    def test_a_stale_client_claim_stops_working_when_the_engagement_ends(
        self, app_client, db, ca_firm, firm_admin, company
    ):
        """The engagement is re-checked on every request, not trusted from the
        token — a twelve-hour token outlives an offboarding."""
        engagement = make_engagement(db, ca_firm, company)
        headers = auth(firm_admin, client_org_id=company.id)
        assert app_client.get(f"{API}/auth/me", headers=headers).status_code == 200

        engagement.status = "terminated"
        db.flush()

        assert app_client.get(f"{API}/auth/me", headers=headers).status_code == 403


class TestPasswordChange:
    def test_the_current_password_must_be_right(self, app_client, company_admin):
        response = app_client.post(
            f"{API}/auth/password",
            headers=auth(company_admin),
            json={"current_password": "wrong", "new_password": "a-brand-new-passphrase"},
        )

        assert response.status_code == 401

    def test_a_changed_password_is_the_one_that_works(self, app_client, company_admin):
        app_client.post(
            f"{API}/auth/password",
            headers=auth(company_admin),
            json={
                "current_password": "correct-horse-battery",
                "new_password": "a-brand-new-passphrase",
            },
        )

        assert _login(app_client, company_admin.email, "correct-horse-battery").status_code == 401
        assert _login(app_client, company_admin.email, "a-brand-new-passphrase").status_code == 200


class TestUserAdministration:
    def test_an_admin_can_add_a_colleague(self, app_client, company_admin):
        response = app_client.post(
            f"{API}/auth/users",
            headers=auth(company_admin),
            json={
                "email": "new.hire@acme.example.com",
                "full_name": "New Hire",
                "password": "a-long-enough-passphrase",
                "role": "staff",
            },
        )

        assert response.status_code == 201
        assert response.json()["role"] == "staff"
        assert response.json()["email"] == "new.hire@acme.example.com"

    def test_the_new_colleague_can_log_in(self, app_client, company_admin):
        app_client.post(
            f"{API}/auth/users",
            headers=auth(company_admin),
            json={
                "email": "new.hire@acme.example.com",
                "full_name": "New Hire",
                "password": "a-long-enough-passphrase",
            },
        )

        assert _login(app_client, "new.hire@acme.example.com", "a-long-enough-passphrase").status_code == 200

    def test_staff_cannot_add_users(self, app_client, company_staff):
        response = app_client.post(
            f"{API}/auth/users",
            headers=auth(company_staff),
            json={
                "email": "new.hire@acme.example.com",
                "full_name": "New Hire",
                "password": "a-long-enough-passphrase",
            },
        )

        assert response.status_code == 403
        assert response.json()["error"]["details"]["required_role"] == "admin"

    def test_a_duplicate_email_within_the_organization_is_a_conflict(
        self, app_client, company_admin
    ):
        response = app_client.post(
            f"{API}/auth/users",
            headers=auth(company_admin),
            json={
                "email": company_admin.email,
                "full_name": "Impostor",
                "password": "a-long-enough-passphrase",
            },
        )

        assert response.status_code == 409

    def test_the_same_email_at_a_different_organization_is_allowed(
        self, app_client, company_admin, other_admin
    ):
        response = app_client.post(
            f"{API}/auth/users",
            headers=auth(company_admin),
            json={
                "email": other_admin.email,
                "full_name": "Same Name Elsewhere",
                "password": "a-long-enough-passphrase",
            },
        )

        assert response.status_code == 201

    def test_listing_returns_only_your_own_organization(
        self, app_client, company_admin, company_staff, other_admin
    ):
        response = app_client.get(f"{API}/auth/users", headers=auth(company_admin))

        ids = {u["id"] for u in response.json()}
        assert ids == {company_admin.id, company_staff.id}

    def test_a_firm_in_a_clients_context_still_lists_its_own_colleagues(
        self, app_client, db, ca_firm, firm_admin, firm_staff, company, company_admin
    ):
        """``home_org_id``, not ``org_id``: a firm has no business listing a
        client's user accounts."""
        make_engagement(db, ca_firm, company)

        response = app_client.get(
            f"{API}/auth/users", headers=auth(firm_admin, client_org_id=company.id)
        )

        ids = {u["id"] for u in response.json()}
        assert ids == {firm_admin.id, firm_staff.id}
        assert company_admin.id not in ids

    def test_a_soft_deleted_user_is_not_listed(self, app_client, db, company_admin, company_staff):
        company_staff.soft_delete()
        db.flush()

        response = app_client.get(f"{API}/auth/users", headers=auth(company_admin))

        assert [u["id"] for u in response.json()] == [company_admin.id]


class TestUserUpdates:
    def test_an_admin_can_change_a_colleagues_role(self, app_client, company_admin, company_staff):
        response = app_client.patch(
            f"{API}/auth/users/{company_staff.id}",
            headers=auth(company_admin),
            json={"role": "compliance_manager"},
        )

        assert response.status_code == 200
        assert response.json()["role"] == "compliance_manager"

    def test_a_role_change_is_logged_as_a_role_change(
        self, app_client, db, company_admin, company_staff
    ):
        """Section 8.2 requires role changes to be distinguishable in the trail."""
        from app.models.audit import AuditTrail

        app_client.patch(
            f"{API}/auth/users/{company_staff.id}",
            headers=auth(company_admin),
            json={"role": "compliance_manager"},
        )

        entry = db.query(AuditTrail).filter_by(action=AuditAction.ROLE_CHANGE).one()
        assert entry.before_json["role"] == "staff"
        assert entry.after_json["role"] == "compliance_manager"

    def test_a_non_role_change_is_logged_as_an_ordinary_update(
        self, app_client, db, company_admin, company_staff
    ):
        from app.models.audit import AuditTrail

        app_client.patch(
            f"{API}/auth/users/{company_staff.id}",
            headers=auth(company_admin),
            json={"full_name": "Rahul V"},
        )

        assert db.query(AuditTrail).filter_by(action=AuditAction.UPDATE).count() == 1
        assert db.query(AuditTrail).filter_by(action=AuditAction.ROLE_CHANGE).count() == 0

    def test_an_admin_cannot_demote_themselves(self, app_client, company_admin):
        """It can strand an organization with no admin and no way back."""
        response = app_client.patch(
            f"{API}/auth/users/{company_admin.id}",
            headers=auth(company_admin),
            json={"role": "staff"},
        )

        assert response.status_code == 403

    def test_an_admin_may_still_edit_their_own_name(self, app_client, company_admin):
        response = app_client.patch(
            f"{API}/auth/users/{company_admin.id}",
            headers=auth(company_admin),
            json={"full_name": "Priya S Sharma"},
        )

        assert response.status_code == 200

    def test_a_user_at_another_organization_is_not_found(
        self, app_client, company_admin, other_admin
    ):
        response = app_client.patch(
            f"{API}/auth/users/{other_admin.id}",
            headers=auth(company_admin),
            json={"full_name": "Renamed By An Outsider"},
        )

        assert response.status_code == 404

    def test_deactivating_a_user_stops_their_existing_token_working(
        self, app_client, company_admin, company_staff
    ):
        headers = auth(company_staff)
        assert app_client.get(f"{API}/auth/me", headers=headers).status_code == 200

        app_client.patch(
            f"{API}/auth/users/{company_staff.id}",
            headers=auth(company_admin),
            json={"is_active": False},
        )

        assert app_client.get(f"{API}/auth/me", headers=headers).status_code == 401


class TestUserRemoval:
    def test_removal_is_a_soft_delete(self, app_client, db, company_admin, company_staff):
        """The row stays so the audit trail's ``user_id`` still resolves."""
        response = app_client.delete(
            f"{API}/auth/users/{company_staff.id}", headers=auth(company_admin)
        )

        assert response.status_code == 200
        db.refresh(company_staff)
        assert company_staff.deleted_at is not None
        assert company_staff.is_active is False

    def test_a_removed_user_cannot_log_in(self, app_client, company_admin, company_staff):
        app_client.delete(f"{API}/auth/users/{company_staff.id}", headers=auth(company_admin))

        assert _login(app_client, company_staff.email, "correct-horse-battery").status_code == 401

    def test_an_admin_cannot_remove_themselves(self, app_client, company_admin):
        response = app_client.delete(
            f"{API}/auth/users/{company_admin.id}", headers=auth(company_admin)
        )

        assert response.status_code == 403

    def test_removing_twice_is_a_404_rather_than_a_second_soft_delete(
        self, app_client, company_admin, company_staff
    ):
        app_client.delete(f"{API}/auth/users/{company_staff.id}", headers=auth(company_admin))

        response = app_client.delete(
            f"{API}/auth/users/{company_staff.id}", headers=auth(company_admin)
        )

        assert response.status_code == 404

    def test_removal_frees_the_email_for_re_use(self, app_client, company_admin, company_staff):
        """Which is the partial unique index doing its job, seen from the API."""
        email = company_staff.email
        app_client.delete(f"{API}/auth/users/{company_staff.id}", headers=auth(company_admin))

        response = app_client.post(
            f"{API}/auth/users",
            headers=auth(company_admin),
            json={
                "email": email,
                "full_name": "Replacement Hire",
                "password": "a-long-enough-passphrase",
            },
        )

        assert response.status_code == 201

    def test_staff_cannot_remove_users(self, app_client, company_staff, company_admin):
        response = app_client.delete(
            f"{API}/auth/users/{company_admin.id}", headers=auth(company_staff)
        )

        assert response.status_code == 403


class TestSecondFactorGuard:
    @pytest.fixture(autouse=True)
    def _mandate_totp(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "require_totp_for_privileged_roles", True)

    def test_the_enrolment_endpoints_stay_reachable_without_a_second_factor(
        self, app_client, company_admin
    ):
        """Otherwise a newly promoted admin could never enrol."""
        response = app_client.post(f"{API}/auth/totp/setup", headers=auth(company_admin))

        assert response.status_code == 200

    def test_an_inactive_organization_locks_out_its_own_admin(
        self, app_client, db, company, company_admin
    ):
        company.is_active = False
        db.flush()

        response = app_client.get(f"{API}/auth/me", headers=auth(company_admin))

        assert response.status_code == 403


class TestNonCaFirmDelegation:
    def test_a_client_claim_on_a_company_users_token_is_refused(
        self, app_client, db, company, company_admin, other_company
    ):
        """A hand-crafted claim must not be honoured just because it decodes."""
        forged = create_access_token(
            company_admin.id,
            org_id=company.id,
            role=str(company_admin.role),
            client_org_id=other_company.id,
        )

        response = app_client.get(
            f"{API}/auth/me", headers={"Authorization": f"Bearer {forged}"}
        )

        assert response.status_code == 403

    def test_an_assignment_can_narrow_the_effective_role_but_not_widen_it(
        self, app_client, db, ca_firm, company
    ):
        """A Staff user handed an ``admin`` grant stays Staff on that client."""
        staff = make_user(db, ca_firm, role=UserRole.STAFF, full_name="Junior")
        engagement = make_engagement(db, ca_firm, company)
        make_assignment(db, staff, engagement, granted_role=UserRole.ADMIN)

        response = app_client.get(
            f"{API}/auth/me", headers=auth(staff, client_org_id=company.id)
        )

        assert response.json()["role"] == "staff"

    def test_a_read_only_grant_caps_a_staff_user_on_that_client(
        self, app_client, db, ca_firm, company
    ):
        staff = make_user(db, ca_firm, role=UserRole.STAFF, full_name="Junior")
        engagement = make_engagement(db, ca_firm, company)
        make_assignment(db, staff, engagement, granted_role=UserRole.READ_ONLY)

        response = app_client.get(
            f"{API}/auth/me", headers=auth(staff, client_org_id=company.id)
        )

        assert response.json()["role"] == "read_only"

    def test_a_firm_admin_acting_for_a_client_cannot_reach_a_third_party(
        self, app_client, db, ca_firm, firm_admin, company, other_company
    ):
        make_engagement(db, ca_firm, company)
        make_org(db, name="Unengaged Ltd")

        response = app_client.get(
            f"{API}/auth/me", headers=auth(firm_admin, client_org_id=other_company.id)
        )

        assert response.status_code == 403
