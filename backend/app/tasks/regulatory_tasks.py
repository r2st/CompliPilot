"""Analysing regulatory updates and mapping them onto tenants (section 4.6).

Two stages, kept separate because they fail differently and cost differently:

1. :func:`analyse_pending_updates` — one model call per *update*, system-wide.
   The CBIC issues one circular; it is analysed once.
2. :func:`map_update_impacts` — no model calls at all. Matching an analysed
   update against each organization's profile is deterministic, so it runs as
   ordinary code over every tenant.

Doing it the other way round — asking a model per tenant per circular — would
be four hundred calls where one will do, and would give four hundred slightly
different answers to the same question.

**A per-tenant impact is a claim about that tenant's obligations**, so the
rationale is recorded in full. A compliance officer will not act on a severity
level they cannot check, and "the model said so" is not something to put in
front of one.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.models.enums import (
    AuditAction,
    ImpactLevel,
    NotificationChannel,
    Regulation,
    UserRole,
)
from app.models.mixins import utcnow
from app.models.organization import Organization
from app.models.regulatory import RegulatoryImpact, RegulatoryUpdate
from app.services import audit as audit_service
from app.services import extraction, llm, notifications
from app.services.applicability import applicable_obligations
from app.tasks.base import SYSTEM_ACTOR, active_organizations, task_session

logger = logging.getLogger(__name__)

# Updates analysed per scheduled run. Bounded because each is a model call on a
# free tier: a backlog drains over several runs rather than hitting the rate
# limit and failing the whole batch.
ANALYSE_BATCH = 20

_REQUIRED = {"summary", "impact_level", "affected_entity_types", "action_required", "confidence"}

_ANALYSIS_PROMPT = """\
Analyse this Indian regulatory update and return JSON with exactly these keys:

- "summary": 2-4 sentences in plain English: what changed, and what it means \
for an affected business.
- "impact_level": one of "critical", "high", "medium", "low". Use "critical" \
only for a change that creates a new obligation or a penalty exposure with a \
near-term deadline.
- "regulation": one of "gst", "income_tax", "rbi", "sebi", "mca", "fema", \
"labour", "dpdp", or null.
- "affected_entity_types": array from "private_limited", "public_limited", \
"llp", "partnership", "proprietorship", "trust", "society". Empty array means \
all entity types.
- "affected_states": array of Indian state names, or empty array for all-India.
- "turnover_threshold_inr": the annual turnover at or above which this applies, \
as a number in rupees, or null if there is no threshold.
- "affected_forms": array of form or return names the update touches \
(e.g. "GSTR-3B", "MGT-7"). Empty array if none named.
- "action_required": one or two sentences stating what an affected business \
must actually do. Null if the update is informational only.
- "compliance_deadline": the date by which action is required, "YYYY-MM-DD", \
or null. Do NOT infer a date that is not stated.
- "confidence": integer 0-100.

Return ONLY the JSON object.

UPDATE:
Title: {title}
Source: {source}
Published: {published}
Reference: {reference}

{content}
"""

_LEVEL_ORDER = {
    ImpactLevel.CRITICAL: 4,
    ImpactLevel.HIGH: 3,
    ImpactLevel.MEDIUM: 2,
    ImpactLevel.LOW: 1,
}

# Impact levels that are worth interrupting someone about. A "low" impact lands
# in the alert list to be read when convenient; emailing every tenant about
# every minor clarification is how a channel gets muted.
NOTIFY_AT_OR_ABOVE = ImpactLevel.HIGH


def _coerce_level(value, default: ImpactLevel = ImpactLevel.MEDIUM) -> ImpactLevel:
    """Read the model's severity, defaulting rather than raising."""
    if isinstance(value, ImpactLevel):
        return value
    if isinstance(value, str):
        try:
            return ImpactLevel(value.strip().lower())
        except ValueError:
            pass
    return default


def _coerce_regulation(value) -> Regulation | None:
    if isinstance(value, Regulation):
        return value
    if isinstance(value, str):
        try:
            return Regulation(value.strip().lower())
        except ValueError:
            return None
    return None


def _parse_date(value) -> date | None:
    """ISO date or nothing. A deadline is never guessed — see document_tasks."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _as_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip().lower() for v in value if str(v).strip()]


def analyse_update(db: Session, update: RegulatoryUpdate) -> bool:
    """Run the model over one update and store the analysis. Flushes.

    Returns whether an analysis was stored. ``is_analysed`` is set either way
    when the model is simply unconfigured, so an un-keyed deployment still
    fans the update out to tenants on title and source alone rather than
    accumulating an unbounded backlog.
    """
    body = extraction.clip_for_model(update.content or update.summary or update.title)

    if not llm.is_configured():
        logger.info("Update %s not analysed: no API key", update.id)
        update.is_analysed = True
        update.analysed_at = utcnow()
        update.analysis_json = {"_skipped": "OpenRouter is not configured"}
        db.flush()
        return False

    result = llm.complete_json(
        _ANALYSIS_PROMPT.format(
            title=update.title,
            source=update.source,
            published=update.published_date.isoformat(),
            reference=update.reference_no or "not stated",
            content=body,
        ),
        required_keys=_REQUIRED,
        max_tokens=1500,
    )
    data = result.data or {}

    update.summary = data.get("summary") or update.summary
    update.impact_level = _coerce_level(data.get("impact_level"))
    update.regulation = _coerce_regulation(data.get("regulation")) or update.regulation
    update.affected_entity_types_json = _as_list(data.get("affected_entity_types"))
    update.affected_states_json = _as_list(data.get("affected_states"))
    update.affected_obligation_codes_json = _as_list(data.get("affected_forms"))
    update.action_required = data.get("action_required")
    update.compliance_deadline = _parse_date(data.get("compliance_deadline"))
    update.analysis_json = {
        **data,
        "_model": result.model,
        "_tokens": result.total_tokens,
        "_confidence": llm.confidence_of(data),
    }
    update.is_analysed = True
    update.analysed_at = utcnow()
    db.flush()
    return True


# --------------------------------------------------------------------------
# Per-tenant impact mapping — deterministic, no model calls
# --------------------------------------------------------------------------


def _rupees_to_paise(rupees) -> int | None:
    if not isinstance(rupees, (int, float)):
        return None
    return int(rupees * 100)


def assess_impact(
    db: Session, org: Organization, update: RegulatoryUpdate
) -> tuple[ImpactLevel, str, list[int]] | None:
    """What *update* means for *org*: ``(level, rationale, obligation_ids)``.

    ``None`` when the update does not apply, which is the common case — a SEBI
    circular is irrelevant to an unlisted proprietorship and putting it on
    their alert list trains them to ignore the list.

    Each filter states its reasoning as it goes, because the rationale is shown
    verbatim to a compliance officer and "does not apply" without a reason is
    not something they can check.
    """
    reasons: list[str] = []

    entity_types = update.affected_entity_types_json or []
    if entity_types:
        if not org.entity_type or str(org.entity_type) not in entity_types:
            return None
        reasons.append(f"entity type {org.entity_type} is named in the update")

    states = update.affected_states_json or []
    if states:
        if not org.state or org.state.strip().lower() not in states:
            return None
        reasons.append(f"state {org.state} is named in the update")

    analysis = update.analysis_json or {}
    threshold = _rupees_to_paise(analysis.get("turnover_threshold_inr"))
    if threshold is not None:
        if org.annual_turnover_paise is None:
            # Unknown turnover against a threshold-bound update is reported at
            # a reduced level rather than dropped. The organization may well be
            # affected, and silence is the one outcome that cannot be checked.
            reasons.append(
                "turnover is not recorded, so the threshold could not be applied"
            )
            return (
                ImpactLevel.LOW,
                "Possibly applicable: " + "; ".join(reasons),
                [],
            )
        if org.annual_turnover_paise < threshold:
            return None
        reasons.append(
            f"turnover ₹{org.annual_turnover_paise / 10_000_000:.2f} Cr is at or above "
            f"the ₹{threshold / 10_000_000:.2f} Cr threshold"
        )

    # Which of this organization's own obligations the update touches. An
    # update that names GSTR-3B matters more to someone who actually files one.
    obligation_ids: list[int] = []
    forms = update.affected_obligation_codes_json or []
    if forms:
        # Via ``applicable_obligations`` rather than a join of our own.
        # ``is_applicable`` is a Python property — the human override falling
        # back to the engine's verdict — so it has no SQL spelling, and
        # duplicating the rule here as a COALESCE would be a second definition
        # of "does this client owe this" that could drift from the first.
        obligation_ids = [
            obligation.id
            for _link, obligation in applicable_obligations(db, org.id)
            if obligation.filing_type and obligation.filing_type.strip().lower() in forms
        ]
        if obligation_ids:
            reasons.append(f"{len(obligation_ids)} of your active obligations are affected")

    if update.regulation and not reasons:
        reasons.append(f"applies to all {update.regulation} filers")

    level = update.impact_level

    # An update that names forms this organization does not file is real but
    # less urgent for them. Stepping it down keeps "critical" meaning something.
    if forms and not obligation_ids:
        level = ImpactLevel.LOW if level != ImpactLevel.CRITICAL else ImpactLevel.MEDIUM
        reasons.append("none of the named forms are on your calendar")

    rationale = "; ".join(reasons) if reasons else "Applies generally"
    return level, rationale.capitalize(), obligation_ids


def _upsert_impact(
    db: Session,
    org: Organization,
    update: RegulatoryUpdate,
    level: ImpactLevel,
    rationale: str,
    obligation_ids: list[int],
) -> tuple[RegulatoryImpact, bool]:
    """Create or refresh the impact row. Returns ``(row, is_new)``.

    Re-running the mapper updates rather than duplicates — the unique index
    would refuse a second row anyway, and an alert list that grew by one entry
    every time the mapper ran would be unusable.
    """
    existing = db.execute(
        select(RegulatoryImpact).where(
            RegulatoryImpact.organization_id == org.id,
            RegulatoryImpact.update_id == update.id,
            RegulatoryImpact.deleted_at.is_(None),
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.impact_level = level
        existing.rationale = rationale
        existing.action_required = update.action_required
        existing.action_deadline = update.compliance_deadline
        existing.affected_obligation_ids_json = obligation_ids
        db.flush()
        return existing, False

    impact = RegulatoryImpact(
        organization_id=org.id,
        update_id=update.id,
        impact_level=level,
        rationale=rationale,
        action_required=update.action_required,
        action_deadline=update.compliance_deadline,
        affected_obligation_ids_json=obligation_ids,
    )
    db.add(impact)
    db.flush()
    return impact, True


def _notify_impact(
    db: Session,
    org: Organization,
    update: RegulatoryUpdate,
    impact: RegulatoryImpact,
) -> None:
    """Tell the organization about a high-impact update, once."""
    if _LEVEL_ORDER[impact.impact_level] < _LEVEL_ORDER[NOTIFY_AT_OR_ABOVE]:
        return
    if impact.is_notified:
        return

    subject = f"[CompliPilot] {str(impact.impact_level).upper()}: {update.title[:200]}"
    lines = [
        org.name,
        "",
        update.title,
        f"Source: {update.source}"
        + (f" — {update.reference_no}" if update.reference_no else ""),
        f"Published: {update.published_date.isoformat()}",
        "",
        update.summary or "",
        "",
        f"Why this reaches you: {impact.rationale}",
    ]
    if impact.action_required:
        lines += ["", f"Action required: {impact.action_required}"]
    if impact.action_deadline:
        lines.append(f"Compliance deadline: {impact.action_deadline.isoformat()}")

    for recipient in notifications.recipients_for(
        db,
        org.id,
        roles={UserRole.ADMIN, UserRole.COMPLIANCE_MANAGER},
        channels={NotificationChannel.EMAIL, NotificationChannel.IN_APP},
    ):
        notifications.send(
            db,
            organization_id=org.id,
            recipient=recipient,
            subject=subject,
            content="\n".join(lines),
            kind="regulatory_alert",
            entity_type="regulatory_update",
            entity_id=update.id,
        )

    impact.is_notified = True
    db.flush()


def map_update_impacts(db: Session, update: RegulatoryUpdate) -> dict:
    """Fan one analysed update out to every organization it affects.

    Commits per organization, for the same reason every other sweep does: one
    tenant with a malformed profile must not cost the other 399 their alert.
    """
    matched = 0
    created = 0

    for org in active_organizations(db):
        try:
            assessment = assess_impact(db, org, update)
            if assessment is None:
                continue
            level, rationale, obligation_ids = assessment

            impact, is_new = _upsert_impact(
                db, org, update, level, rationale, obligation_ids
            )
            _notify_impact(db, org, update, impact)

            if is_new:
                audit_service.record(
                    db,
                    organization_id=org.id,
                    action=AuditAction.NOTIFY,
                    entity_type="regulatory_update",
                    entity_id=update.id,
                    actor_label=SYSTEM_ACTOR,
                    summary=f"Regulatory update assessed as {level} impact",
                    after={"impact_level": str(level), "rationale": rationale},
                )
                created += 1
            matched += 1
            db.commit()
        except Exception:  # noqa: BLE001 - one tenant must not stop the fan-out
            db.rollback()
            logger.exception(
                "Impact mapping failed for org %s on update %s", org.id, update.id
            )

    update.is_published = True
    db.commit()
    return {"update_id": update.id, "matched": matched, "created": created}


@celery_app.task(name="app.tasks.regulatory_tasks.analyse_pending_updates")
def analyse_pending_updates(limit: int = ANALYSE_BATCH) -> dict:
    """Twice daily: analyse new updates, then map each onto tenants."""
    analysed = 0
    mapped = 0

    with task_session() as db:
        pending = (
            db.execute(
                select(RegulatoryUpdate)
                .where(
                    RegulatoryUpdate.is_analysed.is_(False),
                    RegulatoryUpdate.deleted_at.is_(None),
                )
                .order_by(RegulatoryUpdate.published_date.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )

        for update in pending:
            try:
                analyse_update(db, update)
                db.commit()
                analysed += 1
            except llm.UpstreamError as exc:
                # Left unanalysed on purpose, so the next run retries it. The
                # alternative — marking it analysed with no analysis — would
                # bury the circular permanently.
                db.rollback()
                logger.warning("Analysis failed for update %s: %s", update.id, exc)
                continue
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("Analysis crashed for update %s", update.id)
                continue

            result = map_update_impacts(db, update)
            mapped += result["matched"]

    logger.info("Regulatory sweep: %s analysed, %s tenant impacts", analysed, mapped)
    return {"analysed": analysed, "impacts": mapped}


@celery_app.task(name="app.tasks.regulatory_tasks.remap_update")
def remap_update(update_id: int) -> dict:
    """Re-run the tenant fan-out for one update.

    The manual handle for after an analysis is corrected by hand, or after a
    tenant's profile changes in a way that should have matched.
    """
    with task_session() as db:
        update = db.get(RegulatoryUpdate, update_id)
        if update is None or update.deleted_at is not None:
            return {"update_id": update_id, "found": False}
        return map_update_impacts(db, update)
