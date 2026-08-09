"""The management commands: ``python -m app.cli <command>``.

Four commands, and the reason to test them is that nobody is watching when
they run. ``seed`` runs on every deploy, ``verify-audit`` runs from cron, and
``create-admin`` runs once on a fresh box at the point where there is no
account to log in with and no API to complain to. A traceback from any of them
is discovered late and by the wrong person.

So what these tests hold onto is the interface a script sees, not the prose:

**The exit code.** ``verify-audit`` is only useful in cron if a tampered chain
is a non-zero exit — a cron job whose failure mode is "prints something
alarming into a log nobody opens" is not a control. Same for
``create-admin`` refusing a duplicate: the operator who typo'd the
organization name needs the shell to tell them, not the output.

**That the work is committed.** Each command opens its own session and closes
it. A command whose changes are still in a transaction when the process exits
has done nothing, and the printed summary would say otherwise.

**That an out-of-band admin is still an audited event.** An account minted from
a shell, bypassing registration, is precisely what an auditor will ask about.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.cli import main
from app.core.security import verify_password
from app.data.catalogue import coverage_summary
from app.models.audit import AuditTrail
from app.models.enums import AuditAction, OrgType, UserRole
from app.models.obligation import ComplianceObligation
from app.models.organization import Organization
from app.models.user import User
from app.services import audit as audit_service
from tests.conftest import make_org


def run(argv: list[str]) -> int:
    """The command as a shell runs it, returning its exit code."""
    return main(argv)


def printed(capsys) -> str:
    return capsys.readouterr().out


def as_json(capsys) -> dict:
    return json.loads(printed(capsys))


ADMIN_ARGS = [
    "create-admin",
    "--organization",
    "Acme Manufacturing Pvt Ltd",
    "--email",
    "priya@acme.example.com",
    "--name",
    "Priya Sharma",
    "--password",
    "correct-horse-battery",
]


# --------------------------------------------------------------------------
# The parser
# --------------------------------------------------------------------------


class TestParser:
    def test_no_command_is_an_argparse_error(self):
        """Not a traceback, and not a silent success.

        ``python -m app.cli`` in a deploy script that lost its argument must
        stop the deploy.
        """
        with pytest.raises(SystemExit) as exit_info:
            run([])

        assert exit_info.value.code == 2

    def test_an_unknown_command_is_refused(self):
        with pytest.raises(SystemExit) as exit_info:
            run(["migrate-everything"])

        assert exit_info.value.code == 2

    @pytest.mark.parametrize(
        "missing", ["--organization", "--email", "--name", "--password"]
    )
    def test_create_admin_requires_all_four_fields(self, missing):
        """Half an admin is worse than none: it is an account somebody has to
        find and delete."""
        argv = list(ADMIN_ARGS)
        index = argv.index(missing)
        del argv[index : index + 2]

        with pytest.raises(SystemExit) as exit_info:
            run(argv)

        assert exit_info.value.code == 2

    def test_an_organization_type_outside_the_enum_is_refused_before_any_write(self, db):
        with pytest.raises(SystemExit):
            run([*ADMIN_ARGS, "--type", "sole_proprietor_ish"])

        assert db.query(Organization).count() == 0


# --------------------------------------------------------------------------
# catalogue
# --------------------------------------------------------------------------


class TestCatalogue:
    def test_it_prints_the_coverage_counts(self, capsys):
        assert run(["catalogue"]) == 0

        assert as_json(capsys) == coverage_summary()

    def test_the_total_agrees_with_the_per_regulation_counts(self, capsys):
        """The reason this is worth running in CI at all.

        Importing the catalogue validates every spec, so a malformed entry
        fails here rather than at the first seed of the next deploy.
        """
        run(["catalogue"])
        summary = as_json(capsys)

        assert summary["total"] == sum(v for k, v in summary.items() if k != "total")
        assert summary["total"] > 0

    def test_it_does_not_need_a_database(self, capsys, monkeypatch):
        """It reads Python constants. A command that opened a session to print
        them could not be run against a box whose database is down, which is
        one of the times someone wants to check what the deploy will seed."""
        monkeypatch.setattr(
            "app.cli.SessionLocal", lambda: pytest.fail("catalogue opened a session")
        )

        assert run(["catalogue"]) == 0


# --------------------------------------------------------------------------
# seed
# --------------------------------------------------------------------------


class TestSeed:
    def test_it_loads_the_system_catalogue(self, db):
        assert run(["seed"]) == 0

        db.expire_all()
        assert db.query(ComplianceObligation).count() == coverage_summary()["total"]

    def test_the_rows_it_writes_are_system_rows(self, db):
        """``organization_id IS NULL`` is what makes a row shared and
        unwritable. A seed that stamped an owner on them would hand the first
        tenant on the box editing rights over everyone's catalogue."""
        run(["seed"])

        db.expire_all()
        owners = {o.organization_id for o in db.query(ComplianceObligation).all()}
        assert owners == {None}

    def test_it_commits(self, db):
        """The command's session closes when it returns.

        Seeding inside a transaction that is never committed is the failure
        this asserts against, and it would look exactly like success in the
        printed summary.
        """
        run(["seed"])

        db.rollback()
        assert db.query(ComplianceObligation).count() > 0

    def test_it_reports_what_it_changed(self, capsys):
        run(["seed"])

        result = as_json(capsys)
        assert result["obligations_created"] == coverage_summary()["total"]
        assert result["templates_created"] > 0

    def test_running_it_twice_changes_nothing_the_second_time(self, db, capsys):
        """It runs on every deploy, so "idempotent" is the whole contract.

        A second run reporting a hundred updates would mean either the deploy
        is rewriting the catalogue for no reason or the comparison is broken;
        both make the log useless for spotting a real change.
        """
        run(["seed"])
        capsys.readouterr()

        assert run(["seed"]) == 0

        second = as_json(capsys)
        assert set(second.values()) == {0}


# --------------------------------------------------------------------------
# verify-audit
# --------------------------------------------------------------------------


def chain(db, org, *, entries: int = 2):
    for n in range(entries):
        audit_service.record(
            db,
            organization_id=org.id,
            action=AuditAction.UPDATE,
            entity_type="filing",
            entity_id=n,
            summary=f"change {n}",
        )
    db.commit()


class TestVerifyAudit:
    def test_an_intact_chain_exits_zero(self, db, company):
        chain(db, company)

        assert run(["verify-audit"]) == 0

    def test_it_prints_one_line_per_organization(
        self, db, company, other_company, capsys
    ):
        """A line per chain, so a cron log tells you *which* tenant broke
        rather than only that one did."""
        chain(db, company)
        chain(db, other_company)

        run(["verify-audit"])

        lines = [json.loads(line) for line in printed(capsys).strip().splitlines()]
        assert [line["organization_id"] for line in lines] == [company.id, other_company.id]
        assert all(line["is_valid"] for line in lines)

    def test_a_tampered_chain_exits_non_zero(self, db, company):
        """The property the cron entry depends on.

        Printing the failure and exiting 0 would make this a command that
        reports tampering into a log nobody reads.
        """
        chain(db, company)
        (entry,) = db.query(AuditTrail).filter(AuditTrail.sequence == 1).all()
        entry.summary = "rewritten after the fact"
        db.commit()

        assert run(["verify-audit"]) == 1

    def test_the_failure_is_announced_on_stderr(self, db, company, capsys):
        """stdout is a stream of JSON that something may be parsing. The alarm
        goes on the other channel so it does not corrupt it."""
        chain(db, company)
        db.query(AuditTrail).filter(AuditTrail.sequence == 1).one().summary = "rewritten"
        db.commit()

        run(["verify-audit"])
        captured = capsys.readouterr()

        assert "AUDIT CHAIN VERIFICATION FAILED" in captured.err
        assert "FAILED" not in captured.out

    def test_the_break_is_located_in_the_output(self, db, company, capsys):
        chain(db, company, entries=3)
        broken = db.query(AuditTrail).filter(AuditTrail.sequence == 2).one()
        broken.summary = "rewritten"
        db.commit()

        run(["verify-audit"])
        result = json.loads(printed(capsys).strip())

        assert result["is_valid"] is False
        assert result["broken_at_sequence"] == 2

    def test_one_broken_tenant_does_not_stop_the_sweep(
        self, db, company, other_company, capsys
    ):
        """Every chain is checked even after one fails.

        Stopping at the first break would mean a single long-broken tenant hid
        every subsequent one, indefinitely.
        """
        chain(db, company)
        chain(db, other_company)
        db.query(AuditTrail).filter(
            AuditTrail.organization_id == company.id, AuditTrail.sequence == 1
        ).one().summary = "rewritten"
        db.commit()

        assert run(["verify-audit"]) == 1

        results = {
            json.loads(line)["organization_id"]: json.loads(line)["is_valid"]
            for line in printed(capsys).strip().splitlines()
        }
        assert results == {company.id: False, other_company.id: True}

    def test_it_can_be_pointed_at_one_organization(
        self, db, company, other_company, capsys
    ):
        chain(db, company)
        chain(db, other_company)

        run(["verify-audit", "--organization-id", str(other_company.id)])
        lines = printed(capsys).strip().splitlines()

        assert len(lines) == 1
        assert json.loads(lines[0])["organization_id"] == other_company.id

    def test_a_healthy_tenant_is_not_failed_by_a_broken_neighbour(
        self, db, company, other_company
    ):
        chain(db, company)
        chain(db, other_company)
        db.query(AuditTrail).filter(
            AuditTrail.organization_id == company.id, AuditTrail.sequence == 1
        ).one().summary = "rewritten"
        db.commit()

        assert run(["verify-audit", "--organization-id", str(other_company.id)]) == 0

    def test_an_organization_with_no_history_verifies(self, company, capsys):
        """A tenant registered this morning has an empty chain, and an empty
        chain is intact. Failing it would make the first cron run after every
        signup a false alarm."""
        assert run(["verify-audit"]) == 0
        assert json.loads(printed(capsys).strip())["entries_checked"] == 0

    def test_an_id_that_does_not_exist_verifies_vacuously(self, capsys):
        assert run(["verify-audit", "--organization-id", "9999"]) == 0
        assert json.loads(printed(capsys).strip())["entries_checked"] == 0

    def test_with_no_organizations_at_all_it_says_nothing_and_succeeds(self, capsys):
        assert run(["verify-audit"]) == 0
        assert printed(capsys) == ""


# --------------------------------------------------------------------------
# create-admin
# --------------------------------------------------------------------------


class TestCreateAdmin:
    def test_it_creates_the_organization_and_its_admin(self, db, capsys):
        assert run(ADMIN_ARGS) == 0

        db.expire_all()
        org = db.query(Organization).one()
        user = db.query(User).one()
        assert org.name == "Acme Manufacturing Pvt Ltd"
        assert user.organization_id == org.id
        assert user.role == UserRole.ADMIN

    def test_the_password_is_hashed_not_stored(self, db):
        """The one command that takes a password on a command line.

        It is already in the shell history; it must at least not also be in the
        database in clear.
        """
        run(ADMIN_ARGS)

        db.expire_all()
        user = db.query(User).one()
        assert "correct-horse-battery" not in user.password_hash
        assert verify_password("correct-horse-battery", user.password_hash)

    def test_the_email_is_lowercased(self, db):
        """So the account can be logged into.

        Login looks up ``lower(email)``; an address stored with the capitals
        the operator typed would still match, but the organization's contact
        address would disagree with the user's for no reason.
        """
        argv = list(ADMIN_ARGS)
        argv[argv.index("--email") + 1] = "Priya@Acme.Example.com"

        run(argv)

        db.expire_all()
        assert db.query(User).one().email == "priya@acme.example.com"
        assert db.query(Organization).one().contact_email == "priya@acme.example.com"

    def test_the_organization_type_is_settable(self, db):
        run([*ADMIN_ARGS, "--type", "ca_firm"])

        db.expire_all()
        assert db.query(Organization).one().type == OrgType.CA_FIRM

    def test_it_defaults_to_a_company(self, db):
        run(ADMIN_ARGS)

        db.expire_all()
        assert db.query(Organization).one().type == OrgType.COMPANY

    def test_it_commits(self, db):
        run(ADMIN_ARGS)

        db.rollback()
        assert db.query(User).count() == 1

    def test_it_prints_the_ids_it_created(self, db, capsys):
        """An operator's next step is usually to log in as this account or to
        hand the id to somebody. Making them go and query for it is a small
        cruelty on a fresh box with no UI yet."""
        run(ADMIN_ARGS)

        db.expire_all()
        user = db.query(User).one()
        out = printed(capsys)
        assert f"organization {user.organization_id}" in out
        assert f"id {user.id}" in out

    def test_it_says_that_totp_is_still_required(self, db, capsys):
        """The account is created outside registration, so nothing has walked
        the operator through enrolment. In production the first privileged
        action will be refused, and this is the only place to warn them."""
        run(ADMIN_ARGS)

        assert "/api/v1/auth/totp/setup" in printed(capsys)


class TestCreateAdminIsAudited:
    def test_the_creation_lands_in_the_chain(self, db):
        run(ADMIN_ARGS)

        db.expire_all()
        (entry,) = db.query(AuditTrail).all()
        assert entry.action == AuditAction.CREATE
        assert entry.entity_type == "organization"

    def test_the_entry_says_it_came_from_a_shell(self, db):
        """An account minted out of band is what an auditor asks about.

        An entry that looked like an ordinary registration would answer their
        question wrongly.
        """
        run(ADMIN_ARGS)

        db.expire_all()
        (entry,) = db.query(AuditTrail).all()
        assert entry.actor_label == "Priya Sharma (via CLI)"
        assert "from the CLI" in (entry.summary or "")

    def test_the_new_chain_verifies(self, db, capsys):
        """The first entry of a brand-new organization's chain, written by a
        different code path from every other entry, must still link to the
        genesis value the verifier expects."""
        run(ADMIN_ARGS)
        capsys.readouterr()

        assert run(["verify-audit"]) == 0
        assert json.loads(printed(capsys).strip())["entries_checked"] == 1


class TestCreateAdminRefusals:
    def test_an_email_already_in_use_is_refused(self, capsys):
        run(ADMIN_ARGS)
        capsys.readouterr()

        assert run(ADMIN_ARGS) == 1
        assert "already exists" in capsys.readouterr().err

    def test_the_refusal_leaves_no_half_built_organization(self, db):
        """The organization is inserted before the user in the happy path, so
        the check has to happen first — a refusal that had already committed an
        empty organization would leave a tenant nobody can log into."""
        run(ADMIN_ARGS)

        run(ADMIN_ARGS)

        db.expire_all()
        assert db.query(Organization).count() == 1
        assert db.query(User).count() == 1

    def test_a_soft_deleted_account_does_not_block_the_address(self, db, capsys):
        """Which is what makes this a recovery command.

        The organization whose only Admin left, and whose account was
        deactivated, has to be able to get that address back.
        """
        run(ADMIN_ARGS)
        db.expire_all()
        user = db.query(User).one()
        user.deleted_at = user.created_at
        db.commit()
        capsys.readouterr()

        assert run(ADMIN_ARGS) == 0

    def test_an_existing_account_at_another_organization_blocks_it(self, db, capsys):
        """Deliberately stricter than the schema, which is unique per tenant.

        Login has no organization field and takes the lowest-id match, so a
        second account on the same address would be one nobody could reach.
        Refusing here is the difference between a clear error and a mystery.
        """
        org = make_org(db, name="Somebody Else Ltd")
        db.add(
            User(
                organization_id=org.id,
                email="priya@acme.example.com",
                full_name="Priya Sharma",
                password_hash="x",
                role=UserRole.STAFF,
            )
        )
        db.commit()

        assert run(ADMIN_ARGS) == 1

        db.expire_all()
        assert len(db.execute(select(User)).scalars().all()) == 1
