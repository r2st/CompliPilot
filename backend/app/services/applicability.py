"""Deciding which catalogue obligations an organization actually owes.

The catalogue holds ~200 obligations across eight regulators. A five-person
proprietorship in Karnataka owes perhaps twelve of them. This module is what
narrows the first number to the second, by evaluating each obligation's
applicability rules against the organization's profile.

**Every rule is a conjunction, and an unset rule is not a constraint.** An
obligation with ``entity_types_json = None`` applies to every entity type — it
is not "applies to none". That asymmetry is the single most important thing
here, because the opposite reading would make a half-populated catalogue entry
apply to nobody and the omission would never be noticed: no filing generated is
indistinguishable from no filing owed until a penalty notice arrives.

**Missing profile data is not a match.** If an obligation requires turnover
above ₹5 crore and we do not know the organization's turnover, the answer is
"we cannot tell", not "yes" and not "no". Those are returned as
:class:`Verdict` with ``uncertain=True`` so the UI can ask the one question
that resolves them rather than silently guessing. A guess in either direction
is wrong: guessing yes floods a small business with returns it does not owe and
teaches it to ignore the calendar; guessing no is the penalty.

**The engine never overrides a human.** :func:`sync_organization_obligations`
writes its verdict into ``engine_verdict`` and leaves ``is_applicable_override``
alone. A CA who has marked a client exempt from something has almost certainly
seen a fact the profile does not carry, and a nightly re-sync that undid that
decision would be worse than no sync at all.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import Regulation
from app.models.obligation import ComplianceObligation, OrganizationObligation
from app.models.organization import Organization

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Verdict:
    """Whether one obligation applies to one organization, and why.

    ``reason`` is user-facing. It is shown next to the obligation on the
    client's profile, so it names the rule that decided rather than describing
    the code: "Turnover ₹5.0 crore is below the ₹10.0 crore threshold" tells a
    CA something; "rule 3 failed" does not.

    ``uncertain`` marks a verdict reached with a profile field missing. Such a
    verdict is always ``applies=False`` — see the module docstring — but the
    two are distinguished because the UI treats them differently: a false is
    settled, an uncertain is a question.
    """

    applies: bool
    reason: str
    uncertain: bool = False
    # Profile fields that would settle an uncertain verdict, so the UI can ask
    # for exactly those rather than sending the user to a 20-field form.
    missing_fields: tuple[str, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.applies


APPLIES = Verdict(True, "All applicability rules matched")

# ₹1 crore in paise, for the human-readable thresholds in reasons.
_CRORE = 100 * 100_000 * 100


def _crore(paise: int) -> str:
    """Format a paise amount the way an Indian accountant reads it."""
    return f"₹{paise / _CRORE:.2f} crore"


def _in_force(obligation: ComplianceObligation, on: date) -> Verdict | None:
    """Reject an obligation that had not started or has been repealed.

    Checked before anything else: a repealed return must not reappear on next
    year's calendar because the entity type still matches.
    """
    if obligation.effective_from and on < obligation.effective_from:
        return Verdict(
            False,
            f"Not yet in force; effective from {obligation.effective_from.isoformat()}",
        )
    if obligation.effective_to and on > obligation.effective_to:
        return Verdict(
            False, f"No longer in force; ended {obligation.effective_to.isoformat()}"
        )
    return None


def _check_entity_type(org: Organization, allowed: list | None) -> Verdict | None:
    if not allowed:
        return None
    if org.entity_type is None:
        return Verdict(
            False,
            "Entity type is not recorded, and this obligation applies only to "
            + ", ".join(str(a) for a in allowed),
            uncertain=True,
            missing_fields=("entity_type",),
        )
    if str(org.entity_type) not in {str(a) for a in allowed}:
        return Verdict(
            False,
            f"Applies to {', '.join(str(a) for a in allowed)}, not to {org.entity_type}",
        )
    return None


def _check_state(org: Organization, allowed: list | None) -> Verdict | None:
    """State rules, matched case-insensitively.

    Case folding matters more here than elsewhere: state names arrive from
    onboarding forms, CSV imports and the GST portal, and "karnataka" from one
    of them must not read as a different state from "Karnataka".
    """
    if not allowed:
        return None
    if not org.state:
        return Verdict(
            False,
            "State is not recorded, and this obligation is state-specific",
            uncertain=True,
            missing_fields=("state",),
        )
    if org.state.strip().lower() not in {str(a).strip().lower() for a in allowed}:
        return Verdict(False, f"Applies in {', '.join(str(a) for a in allowed)}, not {org.state}")
    return None


def _check_industry(org: Organization, allowed: list | None) -> Verdict | None:
    if not allowed:
        return None
    if not org.industry:
        return Verdict(
            False,
            "Industry is not recorded, and this obligation is industry-specific",
            uncertain=True,
            missing_fields=("industry",),
        )
    if org.industry.strip().lower() not in {str(a).strip().lower() for a in allowed}:
        return Verdict(
            False, f"Applies to {', '.join(str(a) for a in allowed)}, not {org.industry}"
        )
    return None


def _check_turnover(org: Organization, obligation: ComplianceObligation) -> Verdict | None:
    low, high = obligation.min_turnover_paise, obligation.max_turnover_paise
    if low is None and high is None:
        return None
    if org.annual_turnover_paise is None:
        return Verdict(
            False,
            "Annual turnover is not recorded, and this obligation has a turnover threshold",
            uncertain=True,
            missing_fields=("annual_turnover_paise",),
        )

    turnover = org.annual_turnover_paise
    # Inclusive at both ends. A regulator's "turnover exceeding ₹5 crore" is
    # written into the catalogue as a min of ₹5 crore *and one paisa* where the
    # distinction matters; reading the bound as inclusive here means a business
    # exactly on a threshold is warned rather than missed.
    if low is not None and turnover < low:
        return Verdict(
            False, f"Turnover {_crore(turnover)} is below the {_crore(low)} threshold"
        )
    if high is not None and turnover > high:
        return Verdict(
            False, f"Turnover {_crore(turnover)} is above the {_crore(high)} ceiling"
        )
    return None


def _check_employees(org: Organization, minimum: int | None) -> Verdict | None:
    if minimum is None:
        return None
    if org.employee_count is None:
        return Verdict(
            False,
            f"Employee count is not recorded, and this obligation applies from {minimum} employees",
            uncertain=True,
            missing_fields=("employee_count",),
        )
    if org.employee_count < minimum:
        return Verdict(
            False,
            f"{org.employee_count} employees is below the {minimum}-employee threshold",
        )
    return None


def _check_flag(
    actual: bool, required: bool | None, *, positive: str, negative: str
) -> Verdict | None:
    """One of the three boolean profile switches.

    ``required=None`` is no constraint. ``required=True`` demands the flag be
    set, ``required=False`` demands it be clear — the second case is real: some
    exemptions apply only to unlisted companies.

    These three are never uncertain. The columns are non-null with a default,
    so there is no "we do not know whether this company is listed" state to
    represent — unlike turnover, which is genuinely absent until someone types
    it in.
    """
    if required is None or actual == required:
        return None
    return Verdict(False, positive if required else negative)


def evaluate(
    obligation: ComplianceObligation, org: Organization, *, on: date | None = None
) -> Verdict:
    """Whether *org* owes *obligation*.

    Rules are evaluated in order of how cheaply they explain a rejection, so
    the reason a user sees is the most specific true one: telling a
    proprietorship that MGT-7 is for companies is more useful than telling it
    the turnover threshold was not met.
    """
    reference = on or date.today()

    if not obligation.is_active:
        return Verdict(False, "This obligation is no longer in the active catalogue")

    for verdict in (
        _in_force(obligation, reference),
        _check_entity_type(org, obligation.entity_types_json),
        _check_flag(
            org.is_listed,
            obligation.requires_listed,
            positive="Applies only to listed companies",
            negative="Applies only to unlisted companies",
        ),
        _check_flag(
            org.has_foreign_investment,
            obligation.requires_foreign_investment,
            positive="Applies only to entities with foreign investment",
            negative="Applies only to entities without foreign investment",
        ),
        _check_flag(
            org.handles_personal_data,
            obligation.requires_personal_data,
            positive="Applies only to entities that process personal data",
            negative="Applies only to entities that do not process personal data",
        ),
        _check_state(org, obligation.states_json),
        _check_industry(org, obligation.industries_json),
        _check_turnover(org, obligation),
        _check_employees(org, obligation.min_employees),
    ):
        if verdict is not None:
            return verdict

    return APPLIES


@dataclass
class SyncResult:
    """What one :func:`sync_organization_obligations` run changed."""

    evaluated: int = 0
    created: int = 0
    verdict_changed: int = 0
    uncertain: int = 0
    # Rows where the engine disagrees with a standing human override. Not
    # changed — surfaced, so the UI can show "we now think this applies; you
    # have marked it exempt" and a human can revisit it.
    conflicts: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "evaluated": self.evaluated,
            "created": self.created,
            "verdict_changed": self.verdict_changed,
            "uncertain": self.uncertain,
            "conflicts": list(self.conflicts),
        }


def visible_obligations(db: Session, organization_id: int) -> list[ComplianceObligation]:
    """The catalogue as one tenant sees it: system rows plus their own.

    Mirrors :func:`app.core.tenancy.catalogue_scoped`. Written out here rather
    than calling it because this module is also reached from Celery tasks that
    have no :class:`TenantContext` to pass.
    """
    return list(
        db.execute(
            select(ComplianceObligation)
            .where(
                (ComplianceObligation.organization_id.is_(None))
                | (ComplianceObligation.organization_id == organization_id),
                ComplianceObligation.deleted_at.is_(None),
                ComplianceObligation.is_active.is_(True),
            )
            .order_by(ComplianceObligation.regulation, ComplianceObligation.code)
        )
        .scalars()
        .all()
    )


def sync_organization_obligations(
    db: Session,
    org: Organization,
    *,
    on: date | None = None,
    regulations: list[Regulation] | None = None,
) -> SyncResult:
    """Re-evaluate the catalogue for *org* and record the verdicts.

    Creates an :class:`OrganizationObligation` for every catalogue row the
    organization can see, whether or not it applies. Storing the negatives is
    deliberate: "GSTR-9 does not apply to you because your turnover is under
    ₹2 crore" is a thing a CA needs to be able to show a client, and a table
    that held only the positives could not answer it.

    Flushes but does not commit, so the caller controls the transaction —
    the same reason :func:`app.services.audit.record` does.
    """
    result = SyncResult()

    catalogue = visible_obligations(db, org.id)
    if regulations:
        wanted = {str(r) for r in regulations}
        catalogue = [o for o in catalogue if str(o.regulation) in wanted]

    existing = {
        row.obligation_id: row
        for row in db.execute(
            select(OrganizationObligation).where(
                OrganizationObligation.organization_id == org.id,
                OrganizationObligation.deleted_at.is_(None),
            )
        ).scalars()
    }

    for obligation in catalogue:
        verdict = evaluate(obligation, org, on=on)
        result.evaluated += 1
        if verdict.uncertain:
            result.uncertain += 1

        row = existing.get(obligation.id)
        if row is None:
            db.add(
                OrganizationObligation(
                    organization_id=org.id,
                    obligation_id=obligation.id,
                    engine_verdict=verdict.applies,
                    engine_reason=verdict.reason,
                )
            )
            result.created += 1
            continue

        if row.engine_verdict != verdict.applies or row.engine_reason != verdict.reason:
            row.engine_verdict = verdict.applies
            row.engine_reason = verdict.reason
            result.verdict_changed += 1

        if row.is_applicable_override is not None and row.is_applicable_override != verdict.applies:
            result.conflicts.append(obligation.id)

    db.flush()
    logger.info("Applicability sync for org %s: %s", org.id, result.as_dict())
    return result


def applicable_obligations(
    db: Session, organization_id: int
) -> list[tuple[OrganizationObligation, ComplianceObligation]]:
    """The obligations an organization currently owes, with their catalogue rows.

    This is what the filing generator iterates. It reads the *effective*
    verdict — :attr:`OrganizationObligation.is_applicable`, which prefers the
    human override — rather than the engine's, so a client marked exempt stops
    receiving filings immediately rather than at the next sync.
    """
    rows = db.execute(
        select(OrganizationObligation, ComplianceObligation)
        .join(
            ComplianceObligation,
            ComplianceObligation.id == OrganizationObligation.obligation_id,
        )
        .where(
            OrganizationObligation.organization_id == organization_id,
            OrganizationObligation.deleted_at.is_(None),
            ComplianceObligation.deleted_at.is_(None),
            ComplianceObligation.is_active.is_(True),
        )
        .order_by(ComplianceObligation.regulation, ComplianceObligation.code)
    ).all()

    return [(link, obligation) for link, obligation in rows if link.is_applicable]


def coverage_by_regulation(db: Session, organization_id: int) -> dict[str, dict[str, int]]:
    """Per-regulation counts of what applies, what does not, and what is unknown.

    Feeds the profile page's coverage strip. Computed in Python rather than as
    a GROUP BY because the effective verdict involves the override precedence
    above, which is a property of the row rather than a column.
    """
    summary: dict[str, dict[str, int]] = {}
    rows = db.execute(
        select(OrganizationObligation, ComplianceObligation)
        .join(
            ComplianceObligation,
            ComplianceObligation.id == OrganizationObligation.obligation_id,
        )
        .where(
            OrganizationObligation.organization_id == organization_id,
            OrganizationObligation.deleted_at.is_(None),
            ComplianceObligation.deleted_at.is_(None),
        )
    ).all()

    for link, obligation in rows:
        bucket = summary.setdefault(
            str(obligation.regulation),
            {"applicable": 0, "not_applicable": 0, "overridden": 0},
        )
        if link.is_applicable:
            bucket["applicable"] += 1
        else:
            bucket["not_applicable"] += 1
        if link.is_applicable_override is not None:
            bucket["overridden"] += 1

    return summary
