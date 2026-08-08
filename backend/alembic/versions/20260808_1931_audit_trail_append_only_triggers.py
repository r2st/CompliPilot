"""audit trail append only triggers

Section 4.7 requires the audit trail be tamper-proof, and the design document
states plainly that "No UPDATE or DELETE operations are permitted on this
table". The application already never issues either — but a rule enforced only
by the application is a rule that holds until someone opens psql.

These triggers move the guarantee into the database. They fire BEFORE the
statement and raise, so an UPDATE or DELETE against ``audit_trails`` aborts the
transaction it is part of, taking any accompanying cover-up with it.

Two caveats worth being honest about in the file that claims the protection:

* A superuser can drop the trigger. Nothing inside Postgres stops the role that
  owns the schema; the defence against *that* is that dropping a trigger is
  itself a logged DDL event, and that the chain head published by
  :func:`app.services.audit.chain_head` is stored outside this database.
* TRUNCATE is a separate event from DELETE and would otherwise slip past, so it
  gets its own trigger.

Revision ID: 2fe1c31a3c30
Revises: 18476fc943d8
Create Date: 2026-08-08 19:31:46.712063+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "2fe1c31a3c30"
down_revision: str | None = "18476fc943d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The message names the table and the operation, because the person who sees
# it will be surprised and the fastest way to un-surprise them is to say what
# the rule is rather than only that a rule exists.
_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION complipilot_audit_immutable()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'audit_trails is append-only: % is not permitted (see section 4.7)',
        TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite runs the same migration chain in the test suite and has neither
    # plpgsql nor statement-level triggers. The protection there is the
    # application layer plus these tests; skipping keeps one migration history
    # across both databases rather than forking it.
    if bind.dialect.name != "postgresql":
        return

    op.execute(_GUARD_FUNCTION)

    # Row-level for UPDATE and DELETE so the exception names the offending row's
    # operation, and statement-level for TRUNCATE, which has no rows to attach
    # to.
    op.execute(
        """
        CREATE TRIGGER audit_trails_no_update
        BEFORE UPDATE ON audit_trails
        FOR EACH ROW EXECUTE FUNCTION complipilot_audit_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_trails_no_delete
        BEFORE DELETE ON audit_trails
        FOR EACH ROW EXECUTE FUNCTION complipilot_audit_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_trails_no_truncate
        BEFORE TRUNCATE ON audit_trails
        FOR EACH STATEMENT EXECUTE FUNCTION complipilot_audit_immutable();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("DROP TRIGGER IF EXISTS audit_trails_no_truncate ON audit_trails;")
    op.execute("DROP TRIGGER IF EXISTS audit_trails_no_delete ON audit_trails;")
    op.execute("DROP TRIGGER IF EXISTS audit_trails_no_update ON audit_trails;")
    op.execute("DROP FUNCTION IF EXISTS complipilot_audit_immutable();")
