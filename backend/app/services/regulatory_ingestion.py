"""Loading external regulatory updates into the pipeline (section 4.6).

Nothing else in the application ever writes a :class:`RegulatoryUpdate` row.
:mod:`app.tasks.regulatory_tasks` analyses whatever is ``is_analysed = False``
and fans an analysed update out to every tenant it affects — but nothing
upstream of that ever puts a row there in the first place. This module is
what does: a scheduled scraper, or an operator pasting in a circular by hand,
calls :func:`ingest_updates`, and from there the existing pipeline takes over
with no further intervention.

It is called from :mod:`app.cli` rather than exposed as an API route, for the
same reason :mod:`app.data.seed` is a CLI command and not a route:
:class:`RegulatoryUpdate` is system-wide data with no tenant to own it and no
natural actor to authorise the write, and the alternative — trusting any
tenant's Admin to inject rows shown to every other tenant as an official
CBIC circular — is a cross-tenant trust problem this module does not take on.

**Idempotent by (source, reference_no, title)**, matching the model's own
partial unique index (``uq_reg_updates_source_ref``) — re-running an ingest
against the same source, whether from a re-scrape or an operator re-running a
batch file, updates the existing row's content rather than raising or
duplicating it. A record whose content changed is put back in the analysis
queue, because a regulator amending a circular after publication is real and
the old analysis no longer describes it; a record that ingested identically
is left alone, including its ``is_analysed`` flag, so a daily re-scrape does
not re-queue everything it has already seen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import Regulation
from app.models.regulatory import RegulatoryUpdate

# What every record must carry. Everything else is optional and merely
# enriches the row the analyser will read.
REQUIRED_FIELDS = frozenset({"source", "title", "published_date"})

class InvalidUpdateRecord(ValueError):
    """One record in a batch was missing a required field or malformed."""


@dataclass
class IngestResult:
    """What one :func:`ingest_updates` run did. Logged, and returned to the CLI."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    # One message per record that could not be ingested, so a batch of a
    # thousand scraped rows reports exactly which ones need a human rather
    # than failing the whole run on the first bad one.
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "errors": list(self.errors),
        }

    @property
    def total_ingested(self) -> int:
        return self.created + self.updated + self.unchanged


def _parse_date(value, *, field_name: str) -> date:
    if isinstance(value, date):
        return value
    if not value:
        raise InvalidUpdateRecord(f"{field_name} is required")
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise InvalidUpdateRecord(
            f"{field_name} {value!r} is not an ISO date (YYYY-MM-DD)"
        ) from exc


def _parse_regulation(value) -> Regulation | None:
    if not value:
        return None
    try:
        return Regulation(str(value).strip().lower())
    except ValueError as exc:
        raise InvalidUpdateRecord(f"Unknown regulation {value!r}") from exc


def _parse_domains(value) -> list[str] | None:
    if not value:
        return None
    domains = value.split(",") if isinstance(value, str) else value
    out: list[str] = []
    for item in domains:
        item = str(item).strip().lower()
        if not item:
            continue
        try:
            out.append(str(Regulation(item)))
        except ValueError as exc:
            raise InvalidUpdateRecord(f"Unknown domain {item!r}") from exc
    return out or None


def _find_existing(
    db: Session, *, source: str, reference_no: str | None, title: str
) -> RegulatoryUpdate | None:
    """The live row this record refers to, if one already exists.

    Matched in Python rather than left to the database's unique index,
    because the index treats two ``NULL`` reference numbers as distinct rows
    (SQL's ``NULL <> NULL``) — correct for the index's job of refusing an
    accidental collision, wrong for this one: a second ingest of the same
    unnumbered circular must update the first row, not create a sibling.
    """
    stmt = select(RegulatoryUpdate).where(
        RegulatoryUpdate.source == source,
        RegulatoryUpdate.title == title,
        RegulatoryUpdate.deleted_at.is_(None),
    )
    stmt = stmt.where(
        RegulatoryUpdate.reference_no == reference_no
        if reference_no
        else RegulatoryUpdate.reference_no.is_(None)
    )
    return db.execute(stmt).scalar_one_or_none()


def ingest_one(db: Session, record: dict) -> tuple[RegulatoryUpdate, bool, bool]:
    """Create or refresh one update from a plain dict. Flushes.

    Returns ``(row, created, changed)``. ``changed`` is false for a record
    that ingested identically to what is already stored, which is what keeps
    a re-scrape from being indistinguishable from an amendment.
    """
    present = {k for k, v in record.items() if v not in (None, "")}
    missing = REQUIRED_FIELDS - present
    if missing:
        raise InvalidUpdateRecord(f"missing required field(s): {', '.join(sorted(missing))}")

    source = str(record["source"]).strip().lower()
    title = str(record["title"]).strip()
    reference_no = str(record["reference_no"]).strip() if record.get("reference_no") else None
    published_date = _parse_date(record["published_date"], field_name="published_date")
    effective_date = (
        _parse_date(record["effective_date"], field_name="effective_date")
        if record.get("effective_date")
        else None
    )
    regulation = _parse_regulation(record.get("regulation"))
    domains = _parse_domains(record.get("domains") or record.get("domains_json"))

    row = _find_existing(db, source=source, reference_no=reference_no, title=title)
    columns = {
        "source_url": record.get("source_url"),
        "effective_date": effective_date,
        "summary": record.get("summary"),
        "content": record.get("content"),
        "domains_json": domains,
    }

    if row is None:
        row = RegulatoryUpdate(
            source=source,
            title=title,
            reference_no=reference_no,
            published_date=published_date,
            regulation=regulation,
            **columns,
        )
        db.add(row)
        db.flush()
        return row, True, True

    changed = False
    if row.published_date != published_date:
        row.published_date = published_date
        changed = True
    if regulation is not None and row.regulation != regulation:
        row.regulation = regulation
        changed = True
    for key, value in columns.items():
        if value is not None and getattr(row, key) != value:
            setattr(row, key, value)
            changed = True

    if changed:
        # Content that changed invalidates whatever analysis was run against
        # the old text, so the row goes back in the queue the sweep selects
        # from (``is_analysed = False``) rather than keeping a stale one.
        row.is_analysed = False
        row.analysed_at = None
        row.is_published = False

    db.flush()
    return row, False, changed


def ingest_updates(db: Session, records: list[dict]) -> IngestResult:
    """Ingest a batch of records.

    Commits per record — the same isolation every sweep in this codebase
    uses — so one malformed row from a thousand-row scrape is reported in
    ``errors`` rather than losing the other nine hundred and ninety-nine.
    """
    result = IngestResult()
    for index, record in enumerate(records):
        try:
            _row, created, changed = ingest_one(db, record)
            db.commit()
        except InvalidUpdateRecord as exc:
            db.rollback()
            result.errors.append(f"record {index}: {exc}")
            continue
        if created:
            result.created += 1
        elif changed:
            result.updated += 1
        else:
            result.unchanged += 1
    return result
