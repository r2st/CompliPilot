"""Upsert the system catalogue and template library into the database.

Idempotent by design and run on every deploy. That is what makes a catalogue
correction — a due date the regulator moved, a threshold the Finance Act
changed — a code change that ships through the normal pipeline rather than a
hand-written UPDATE run against production by whoever is on call.

**What it will and will not touch.** Only rows with ``is_system = true`` and
``organization_id IS NULL``. A CA firm's own obligations and templates are never
read, never updated and never deleted by this. A system row that disappears from
the catalogue is *deactivated*, not deleted: filings already generated against it
still have to resolve their ``obligation_id``, and a client mid-assessment needs
to be able to see the rule as it stood.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.catalogue import ALL_OBLIGATIONS
from app.data.templates import SYSTEM_TEMPLATES
from app.models.obligation import ComplianceObligation
from app.models.template import Template

logger = logging.getLogger(__name__)


@dataclass
class SeedResult:
    """What one seed run changed. Logged, and returned to the CLI."""

    obligations_created: int = 0
    obligations_updated: int = 0
    obligations_deactivated: int = 0
    templates_created: int = 0
    templates_updated: int = 0
    templates_deactivated: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "obligations_created": self.obligations_created,
            "obligations_updated": self.obligations_updated,
            "obligations_deactivated": self.obligations_deactivated,
            "templates_created": self.templates_created,
            "templates_updated": self.templates_updated,
            "templates_deactivated": self.templates_deactivated,
        }

    @property
    def total_changes(self) -> int:
        return sum(self.as_dict().values())


def _apply(row, columns: dict, *, skip: frozenset[str] = frozenset()) -> bool:
    """Copy *columns* onto *row*, returning whether anything actually changed.

    The return value is what keeps the log honest: without it every run reports
    "updated 122 obligations" and nobody reads the line again.
    """
    changed = False
    for key, value in columns.items():
        if key in skip:
            continue
        if getattr(row, key) != value:
            setattr(row, key, value)
            changed = True
    return changed


def seed_obligations(db: Session) -> SeedResult:
    """Upsert the system obligations catalogue."""
    result = SeedResult()

    existing = {
        row.code: row
        for row in db.execute(
            select(ComplianceObligation).where(
                ComplianceObligation.organization_id.is_(None),
                ComplianceObligation.is_system.is_(True),
            )
        ).scalars()
    }

    for spec in ALL_OBLIGATIONS:
        columns = spec.to_columns()
        row = existing.pop(spec.code, None)
        if row is None:
            db.add(ComplianceObligation(**columns))
            result.obligations_created += 1
            continue
        # ``code`` is the identity we matched on; writing it back would be a
        # no-op at best and, if it ever differed, a silent re-key.
        if _apply(row, columns, skip=frozenset({"code", "organization_id"})):
            result.obligations_updated += 1
        # A row that was deactivated by an earlier run and has since come back
        # into the catalogue is reactivated rather than duplicated.
        if row.deleted_at is not None:
            row.deleted_at = None
            result.obligations_updated += 1

    # Whatever is left in ``existing`` is no longer in the catalogue.
    for orphan in existing.values():
        if orphan.is_active:
            orphan.is_active = False
            result.obligations_deactivated += 1

    db.flush()
    return result


def seed_templates(db: Session) -> SeedResult:
    """Upsert the system template library.

    Version handling differs from the obligations. A template's ``version`` is
    part of its identity — a filing points at the row it was drafted against —
    so a changed field schema is a *new row at the next version*, not an edit to
    the existing one. Editing in place would silently re-render a filing someone
    submitted last year against fields that did not exist then.
    """
    result = SeedResult()

    rows = list(
        db.execute(
            select(Template).where(
                Template.organization_id.is_(None), Template.is_system.is_(True)
            )
        ).scalars()
    )

    # Highest live version per code.
    latest: dict[str, Template] = {}
    for row in rows:
        current = latest.get(row.code)
        if current is None or row.version > current.version:
            latest[row.code] = row

    seen: set[str] = set()
    for spec in SYSTEM_TEMPLATES:
        seen.add(spec.code)
        columns = spec.to_columns()
        row = latest.get(spec.code)

        if row is None:
            db.add(Template(**columns))
            result.templates_created += 1
            continue

        content_changed = (
            row.template_json != columns["template_json"]
            or row.body_template != columns["body_template"]
        )
        if content_changed:
            # New version, and the old one stops being offered for new filings
            # while remaining resolvable for the ones that already point at it.
            row.is_active = False
            db.add(Template(**{**columns, "version": row.version + 1}))
            result.templates_created += 1
            continue

        # Presentation-only changes — a clearer name, better instructions — are
        # safe to apply in place, because nothing is rendered from them.
        if _apply(
            row,
            columns,
            skip=frozenset(
                {"code", "organization_id", "version", "template_json", "body_template"}
            ),
        ):
            result.templates_updated += 1

    for code, row in latest.items():
        if code not in seen and row.is_active:
            row.is_active = False
            result.templates_deactivated += 1

    db.flush()
    return result


def seed_all(db: Session, *, commit: bool = True) -> SeedResult:
    """Seed everything. Returns a combined :class:`SeedResult`.

    ``commit=False`` lets the test suite seed inside a transaction it will roll
    back, and lets a caller batch the seed with other setup in one transaction.
    """
    obligations = seed_obligations(db)
    templates = seed_templates(db)

    combined = SeedResult(
        obligations_created=obligations.obligations_created,
        obligations_updated=obligations.obligations_updated,
        obligations_deactivated=obligations.obligations_deactivated,
        templates_created=templates.templates_created,
        templates_updated=templates.templates_updated,
        templates_deactivated=templates.templates_deactivated,
    )

    if commit:
        db.commit()

    if combined.total_changes:
        logger.info("Seed applied: %s", combined.as_dict())
    else:
        logger.info("Seed: catalogue and templates already current")
    return combined
