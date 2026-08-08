"""partial unique indexes for soft delete

The initial schema expressed "unique among rows that are not soft deleted" as a
composite ``UNIQUE (cols..., deleted_at)``. That does not do it. SQL treats two
NULLs as distinct, and every live row has ``deleted_at IS NULL`` — so the
constraint compared live rows against nothing and fired only if two rows were
soft deleted at the identical microsecond.

The practical effect was that none of these held:

* two live organizations could share a GSTIN or a CIN;
* two users in one organization could share an email address;
* the filing generator's idempotency guarantee — "running the sweep twice for
  July cannot produce two GSTR-3Bs" — rested on a constraint that never fired;
* a CA firm could hold two live engagements with the same client.

This migration replaces each with a partial unique index over
``WHERE deleted_at IS NULL``, which says what was meant. The soft-delete
re-onboarding behaviour the original constraints were reaching for is preserved:
deleting a row takes it out of the index's subset and frees its identifier.

The new index reuses the old constraint's name, so each pair is dropped and
recreated in order — index and constraint names share one namespace in Postgres.

**Before running this against a database with real rows**, check for duplicates.
The index creation will fail on any that exist, which is the correct outcome —
a duplicate GSTIN is two records of one company and merging them is a decision,
not a migration step. The query for each is::

    SELECT gstin_fingerprint, count(*) FROM organizations
    WHERE deleted_at IS NULL AND gstin_fingerprint IS NOT NULL
    GROUP BY 1 HAVING count(*) > 1;

Revision ID: 7c4a91b2e5d8
Revises: 2fe1c31a3c30
Create Date: 2026-08-08 22:10:04.118872+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c4a91b2e5d8"
down_revision: str | None = "2fe1c31a3c30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (table, constraint/index name, columns). The columns are the *real* key —
# ``deleted_at`` is dropped from the tuple because it moves into the predicate.
_KEYS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("organizations", "uq_organizations_gstin", ("gstin_fingerprint",)),
    ("organizations", "uq_organizations_cin", ("cin_fingerprint",)),
    ("clients", "uq_clients_firm_client", ("ca_firm_id", "client_org_id")),
    ("users", "uq_users_org_email", ("organization_id", "email")),
    ("client_assignments", "uq_assignment_user_client", ("user_id", "client_id")),
    ("compliance_obligations", "uq_obligations_org_code", ("organization_id", "code")),
    ("organization_obligations", "uq_org_obligation", ("organization_id", "obligation_id")),
    (
        "filings",
        "uq_filings_org_obligation_period",
        ("organization_id", "obligation_id", "period_key"),
    ),
    ("deadlines", "uq_deadlines_filing", ("filing_id",)),
    ("templates", "uq_templates_code_version", ("organization_id", "code", "version")),
    ("regulatory_updates", "uq_reg_updates_source_ref", ("source", "reference_no", "title")),
    ("regulatory_impacts", "uq_impact_org_update", ("organization_id", "update_id")),
    ("notification_preferences", "uq_notif_pref_org_user", ("organization_id", "user_id")),
    (
        "consent_records",
        "uq_consent_principal_purpose_version",
        ("organization_id", "principal_fingerprint", "purpose", "notice_version"),
    ),
    (
        "data_map_entries",
        "uq_datamap_system_category",
        ("organization_id", "system_name", "data_category"),
    ),
    ("breach_incidents", "uq_breach_org_reference", ("organization_id", "reference")),
    ("data_subject_requests", "uq_dsr_org_ref", ("organization_id", "reference")),
)

# ``sa.text`` rather than a bare string: SQLAlchemy compiles the predicate as a
# SQL expression, and hands a plain str straight to the compiler, which cannot
# dispatch on it.
_PREDICATE = sa.text("deleted_at IS NULL")


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    for table, name, columns in _KEYS:
        # SQLite cannot drop a constraint in place; batch mode rewrites the
        # table. Postgres drops it directly.
        if dialect == "sqlite":
            with op.batch_alter_table(table) as batch:
                batch.drop_constraint(name, type_="unique")
        else:
            op.drop_constraint(name, table, type_="unique")

        op.create_index(
            name,
            table,
            list(columns),
            unique=True,
            postgresql_where=_PREDICATE,
            sqlite_where=_PREDICATE,
        )


def downgrade() -> None:
    """Restore the original constraints.

    Honest warning: this reinstates the bug. It exists so the revision can be
    stepped back cleanly, not because the previous state was correct. Going back
    can also fail where the partial index has since prevented duplicates that
    the old constraint would have allowed — nothing is lost by that, but the
    downgrade will stop.
    """
    dialect = op.get_bind().dialect.name

    for table, name, columns in reversed(_KEYS):
        op.drop_index(name, table_name=table)
        restored = [*columns, "deleted_at"]
        if dialect == "sqlite":
            with op.batch_alter_table(table) as batch:
                batch.create_unique_constraint(name, restored)
        else:
            op.create_unique_constraint(name, table, restored)
