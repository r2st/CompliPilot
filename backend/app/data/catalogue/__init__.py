"""The system obligations catalogue, one module per regulatory domain.

:data:`ALL_OBLIGATIONS` is the whole catalogue. Everything else in the product
that needs "what obligations exist" reads it from here rather than from the
database, so the regulatory test suite of section 9.2 can assert against the
rules without a Postgres instance.

**On the count.** The design document claims coverage of "over 200 specific
compliance obligations". This catalogue ships fewer than that — the entries here
are the ones whose statutory anchor, due-date rule and applicability could each
be stated precisely, and a catalogue row that is nearly right produces a
deadline that is exactly wrong. The remainder are reachable through the same
mechanisms without a code change: a CA firm adds its own with
``organization_id`` set, and the regulatory pipeline proposes new ones from the
gazette. :func:`coverage_summary` reports the real number rather than the
brochure one.
"""
from __future__ import annotations

from app.data.catalogue.base import ObligationSpec
from app.data.catalogue.dpdp import DPDP_OBLIGATIONS
from app.data.catalogue.fema import FEMA_OBLIGATIONS
from app.data.catalogue.gst import GST_OBLIGATIONS
from app.data.catalogue.income_tax import INCOME_TAX_OBLIGATIONS
from app.data.catalogue.labor import LABOR_OBLIGATIONS
from app.data.catalogue.mca import MCA_OBLIGATIONS
from app.data.catalogue.rbi import RBI_OBLIGATIONS
from app.data.catalogue.sebi import SEBI_OBLIGATIONS
from app.models.enums import Regulation

ALL_OBLIGATIONS: tuple[ObligationSpec, ...] = (
    *GST_OBLIGATIONS,
    *INCOME_TAX_OBLIGATIONS,
    *MCA_OBLIGATIONS,
    *RBI_OBLIGATIONS,
    *SEBI_OBLIGATIONS,
    *FEMA_OBLIGATIONS,
    *LABOR_OBLIGATIONS,
    *DPDP_OBLIGATIONS,
)


def _build_index() -> dict[str, ObligationSpec]:
    """Map code → spec, refusing a duplicate code.

    A duplicate would make the seed's upsert non-deterministic — whichever
    entry ran last would win — and would break every join that treats ``code``
    as the stable machine key. Caught at import, which is the only place it can
    be caught before it reaches a database.
    """
    index: dict[str, ObligationSpec] = {}
    for spec in ALL_OBLIGATIONS:
        if spec.code in index:
            raise ValueError(f"Duplicate obligation code in the catalogue: {spec.code}")
        index[spec.code] = spec
    return index


BY_CODE: dict[str, ObligationSpec] = _build_index()


def by_regulation(regulation: Regulation) -> tuple[ObligationSpec, ...]:
    return tuple(spec for spec in ALL_OBLIGATIONS if spec.regulation == regulation)


def coverage_summary() -> dict[str, int]:
    """How many obligations each domain contributes. Surfaced by the API."""
    counts = {str(reg): 0 for reg in Regulation}
    for spec in ALL_OBLIGATIONS:
        counts[str(spec.regulation)] += 1
    counts["total"] = len(ALL_OBLIGATIONS)
    return counts


__all__ = [
    "ALL_OBLIGATIONS",
    "BY_CODE",
    "DPDP_OBLIGATIONS",
    "FEMA_OBLIGATIONS",
    "GST_OBLIGATIONS",
    "INCOME_TAX_OBLIGATIONS",
    "LABOR_OBLIGATIONS",
    "MCA_OBLIGATIONS",
    "RBI_OBLIGATIONS",
    "SEBI_OBLIGATIONS",
    "ObligationSpec",
    "by_regulation",
    "coverage_summary",
]
