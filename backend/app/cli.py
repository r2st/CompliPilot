"""Management commands: ``python -m app.cli <command>``.

Kept to operations that genuinely have no home in the API — seeding the system
catalogue, verifying an audit chain, minting the first administrator on a fresh
deployment. Anything a user can do, they do through the API, so that it is
authenticated, rate limited and recorded in the audit trail.

The three commands here are the exceptions, and each is an exception for a
reason stated at its definition.
"""
from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.logging import configure_logging
from app.core.security import hash_password
from app.models.enums import AuditAction, OrgType, UserRole
from app.models.organization import Organization
from app.models.user import User
from app.services import audit as audit_service


def cmd_seed(args: argparse.Namespace) -> int:
    """Upsert the system obligations catalogue and template library.

    Run on every deploy. Idempotent — see :mod:`app.data.seed`.
    """
    from app.data.seed import seed_all

    with SessionLocal() as db:
        result = seed_all(db)
    print(json.dumps(result.as_dict(), indent=2))
    return 0


def cmd_catalogue(args: argparse.Namespace) -> int:
    """Print the catalogue's coverage without touching the database.

    Useful in CI: it fails if any entry in the catalogue is malformed, because
    importing the module runs every spec's ``validate``.
    """
    from app.data.catalogue import coverage_summary

    print(json.dumps(coverage_summary(), indent=2))
    return 0


def cmd_verify_audit(args: argparse.Namespace) -> int:
    """Recompute one organization's audit chain, or every organization's.

    Exists as a command as well as an endpoint because the most valuable time
    to run it is from cron, into a log nobody has to remember to open — and
    because if the API is the only way to check the audit trail, a compromised
    API can answer "all fine".
    """
    from app.services.audit import verify_chain

    exit_code = 0
    with SessionLocal() as db:
        if args.organization_id:
            org_ids = [args.organization_id]
        else:
            org_ids = list(
                db.execute(select(Organization.id).order_by(Organization.id)).scalars()
            )

        for org_id in org_ids:
            result = verify_chain(db, org_id)
            print(json.dumps(result.as_dict()))
            if not result.is_valid:
                exit_code = 1

    if exit_code:
        print("AUDIT CHAIN VERIFICATION FAILED", file=sys.stderr)
    return exit_code


def cmd_create_admin(args: argparse.Namespace) -> int:
    """Create an organization and its first Admin.

    The registration endpoint does the same thing and is the normal route. This
    exists for the first account on a fresh deployment, where registration may
    be closed, and for recovering an organization whose only Admin left.
    """
    with SessionLocal() as db:
        existing = db.execute(
            select(User).where(User.email == args.email.lower(), User.deleted_at.is_(None))
        ).scalar_one_or_none()
        if existing is not None:
            print(f"A user with {args.email} already exists (id {existing.id})", file=sys.stderr)
            return 1

        org = Organization(
            name=args.organization,
            type=OrgType(args.type),
            contact_email=args.email.lower(),
        )
        db.add(org)
        db.flush()

        user = User(
            organization_id=org.id,
            email=args.email.lower(),
            full_name=args.name,
            password_hash=hash_password(args.password),
            role=UserRole.ADMIN,
        )
        db.add(user)
        db.flush()

        # Recorded like any other creation. An account minted out of band is
        # exactly the kind of event an auditor will ask about, so it goes in the
        # chain with an actor label that says where it came from.
        audit_service.record(
            db,
            organization_id=org.id,
            action=AuditAction.CREATE,
            entity_type="organization",
            entity_id=org.id,
            user_id=user.id,
            actor_label=f"{user.full_name} (via CLI)",
            after={"name": org.name, "type": str(org.type)},
            summary=f"Organization {org.name} and its first admin created from the CLI",
        )
        db.commit()
        print(f"Created organization {org.id} and admin {user.email} (id {user.id})")

    if user.role in {UserRole.ADMIN}:
        print(
            "Two-factor authentication is mandatory for this role in production; "
            "enrol at /api/v1/auth/totp/setup before the first privileged action."
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="CompliPilot management commands")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="Upsert the system catalogue and templates").set_defaults(
        func=cmd_seed
    )
    sub.add_parser("catalogue", help="Print catalogue coverage").set_defaults(func=cmd_catalogue)

    verify = sub.add_parser("verify-audit", help="Verify audit chain integrity")
    verify.add_argument(
        "--organization-id", type=int, default=None, help="One organization; default is all"
    )
    verify.set_defaults(func=cmd_verify_audit)

    admin = sub.add_parser("create-admin", help="Create an organization and its first admin")
    admin.add_argument("--organization", required=True)
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", required=True)
    admin.add_argument("--password", required=True)
    admin.add_argument("--type", default="company", choices=[t.value for t in OrgType])
    admin.set_defaults(func=cmd_create_admin)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
