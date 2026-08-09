"""The filing lifecycle: which status transitions are legal, and who may make them.

:mod:`app.models.enums` defines the vocabulary; this module defines the grammar.
Keeping them apart means a status can be read anywhere without dragging in the
rules, and the rules exist in exactly one place rather than as an ``if`` in each
of the four routes that move a filing along.

**Why a transition table rather than checks at the call sites.** The illegal
moves are the interesting ones and they are easy to miss one at a time:
re-submitting an acknowledged return, approving your own draft, reopening a
filing whose acknowledgement number is already with the portal. Written as a
table, "what can this filing do next" is answerable by reading one dict, and the
API can *tell* the client the answer instead of making it guess and get a 409.

**Separation of preparer and approver.** Section 4.2 requires human review of
every AI-generated draft. A review the preparer performs on their own work is
not a review, so :func:`check_transition` refuses it — with the deliberate
exception of a single-user organization, where the alternative is that a
one-person practice can never file anything.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ForbiddenError, InvalidTransitionError
from app.core.tenancy import TenantContext
from app.models.enums import FilingStatus, UserRole
from app.models.filing import Deadline, Filing
from app.models.mixins import utcnow
from app.models.user import User

logger = logging.getLogger(__name__)


# Which statuses each status may move to. A status absent from a value list is
# unreachable from that key — that is the whole enforcement mechanism.
TRANSITIONS: dict[FilingStatus, frozenset[FilingStatus]] = {
    FilingStatus.NOT_STARTED: frozenset(
        {FilingStatus.DRAFT, FilingStatus.NOT_APPLICABLE}
    ),
    FilingStatus.DRAFT: frozenset(
        {FilingStatus.IN_REVIEW, FilingStatus.NOT_APPLICABLE}
    ),
    FilingStatus.IN_REVIEW: frozenset(
        # Back to DRAFT is a rejection at review: the reviewer wants changes,
        # which is an ordinary outcome and not the same as REJECTED, which
        # means the *portal* refused it.
        {FilingStatus.APPROVED, FilingStatus.DRAFT}
    ),
    FilingStatus.APPROVED: frozenset(
        # Approved work can be sent back to draft — an approval given before
        # someone noticed a wrong figure has to be revocable while the filing
        # is still in our hands.
        {FilingStatus.SUBMITTED, FilingStatus.LATE_FILED, FilingStatus.DRAFT}
    ),
    FilingStatus.SUBMITTED: frozenset(
        {FilingStatus.ACKNOWLEDGED, FilingStatus.REJECTED}
    ),
    FilingStatus.LATE_FILED: frozenset(
        {FilingStatus.ACKNOWLEDGED, FilingStatus.REJECTED}
    ),
    # A portal rejection goes back to the desk to be fixed and refiled.
    FilingStatus.REJECTED: frozenset({FilingStatus.DRAFT}),
    # Terminal. An acknowledgement number is the regulator's receipt; a filing
    # that carries one is history, and correcting it is a revised return —
    # a new filing, not an edit to this one.
    FilingStatus.ACKNOWLEDGED: frozenset(),
    FilingStatus.NOT_APPLICABLE: frozenset({FilingStatus.NOT_STARTED}),
}

# The minimum role each transition needs. Anything unlisted needs STAFF, which
# is the floor for any write at all.
_REQUIRED_ROLE: dict[FilingStatus, UserRole] = {
    FilingStatus.APPROVED: UserRole.COMPLIANCE_MANAGER,
    FilingStatus.SUBMITTED: UserRole.COMPLIANCE_MANAGER,
    FilingStatus.LATE_FILED: UserRole.COMPLIANCE_MANAGER,
    # Marking something not owed is a judgement with a penalty attached if it
    # is wrong, so it sits at the same level as approving.
    FilingStatus.NOT_APPLICABLE: UserRole.COMPLIANCE_MANAGER,
}

# Transitions after which the deadline row stops chasing anyone.
_SATISFYING = frozenset(
    {
        FilingStatus.SUBMITTED,
        FilingStatus.LATE_FILED,
        FilingStatus.ACKNOWLEDGED,
        FilingStatus.NOT_APPLICABLE,
    }
)


def allowed_from(status: FilingStatus) -> list[FilingStatus]:
    """The statuses reachable from *status*, sorted for a stable API response."""
    return sorted(TRANSITIONS.get(status, frozenset()), key=str)


def _is_sole_operator(db: Session, organization_id: int) -> bool:
    """Whether this organization has only one person who could review.

    Counts users at Compliance Manager or above, because those are the only
    ones who can approve. A firm with ten staff and one manager still has one
    approver, and requiring a second would block them entirely.
    """
    total = db.execute(
        select(func.count())
        .select_from(User)
        .where(
            User.organization_id == organization_id,
            User.deleted_at.is_(None),
            User.is_active.is_(True),
            User.role.in_([UserRole.ADMIN, UserRole.COMPLIANCE_MANAGER]),
        )
    ).scalar_one()
    return int(total) <= 1


def check_transition(
    db: Session, filing: Filing, target: FilingStatus, ctx: TenantContext
) -> None:
    """Raise unless *ctx* may move *filing* to *target*.

    Three gates, in the order that gives the most useful error: is the move
    legal at all, does the actor hold the role for it, and — for an approval —
    is the actor someone other than the person who prepared it.
    """
    current = filing.status

    if target == current:
        raise InvalidTransitionError(
            f"This filing is already {target}",
            details={"current": str(current), "allowed": [str(s) for s in allowed_from(current)]},
        )

    if target not in TRANSITIONS.get(current, frozenset()):
        raise InvalidTransitionError(
            f"A filing that is {current} cannot become {target}",
            details={
                "current": str(current),
                "requested": str(target),
                "allowed": [str(s) for s in allowed_from(current)],
            },
        )

    required = _REQUIRED_ROLE.get(target, UserRole.STAFF)
    if not ctx.at_least(required):
        raise ForbiddenError(
            f"Moving a filing to {target} requires the {required} role",
            details={"required_role": str(required), "your_role": str(ctx.role)},
        )

    if (
        target == FilingStatus.APPROVED
        and filing.prepared_by_id is not None
        and filing.prepared_by_id == ctx.user_id
        and not _is_sole_operator(db, filing.organization_id)
    ):
        raise ForbiddenError(
            "A filing must be approved by someone other than the person who prepared it",
            details={"prepared_by_id": filing.prepared_by_id},
        )


@dataclass(frozen=True)
class TransitionOutcome:
    """What a completed transition did, for the audit summary and the response."""

    filing_id: int
    previous: FilingStatus
    current: FilingStatus
    was_late: bool = False
    deadline_satisfied: bool = False


def apply_transition(
    db: Session,
    filing: Filing,
    target: FilingStatus,
    ctx: TenantContext,
    *,
    acknowledgement_no: str | None = None,
    rejection_reason: str | None = None,
    notes: str | None = None,
    today: date | None = None,
) -> TransitionOutcome:
    """Move *filing* to *target*, setting the bookkeeping that goes with it.

    Validates first, then writes. The side effects — who reviewed it and when,
    whether it counted as late, whether the deadline stops chasing — are here
    rather than in the route because every one of them must happen on every
    path that reaches this status, and a route is exactly the place someone
    will add a fifth path and forget one.

    Flushes, does not commit. The caller's transaction owns this, so the audit
    entry it writes afterwards lands or rolls back with the change.
    """
    check_transition(db, filing, target, ctx)
    reference = today or date.today()
    previous = filing.status

    # SUBMITTED past the deadline is silently recorded as LATE_FILED. Letting
    # it stand as SUBMITTED would hide a penalty exposure from the very report
    # a CA runs to find them.
    was_late = False
    if target == FilingStatus.SUBMITTED and reference > filing.effective_due_date:
        target = FilingStatus.LATE_FILED
        was_late = True

    filing.status = target

    # Whoever pushed it into review is the preparer of record, unless one was
    # already set by the generator or an earlier draft.
    if target == FilingStatus.IN_REVIEW and filing.prepared_by_id is None:
        filing.prepared_by_id = ctx.user_id

    if target == FilingStatus.APPROVED:
        filing.reviewed_by_id = ctx.user_id
        filing.reviewed_at = utcnow()
        filing.rejection_reason = None

    if target in (FilingStatus.SUBMITTED, FilingStatus.LATE_FILED):
        filing.submitted_at = utcnow()
        filing.submitted_by_id = ctx.user_id

    if target == FilingStatus.ACKNOWLEDGED:
        # The acknowledgement number is the point of this status. Refusing the
        # transition without one keeps the terminal state from being reached
        # with nothing to show for it at an assessment.
        if not acknowledgement_no and not filing.acknowledgement_no:
            raise InvalidTransitionError(
                "An acknowledgement number is required to mark a filing acknowledged",
                details={"field": "acknowledgement_no"},
            )
        if acknowledgement_no:
            filing.acknowledgement_no = acknowledgement_no

    if target == FilingStatus.REJECTED:
        if not rejection_reason:
            raise InvalidTransitionError(
                "A rejection reason is required",
                details={"field": "rejection_reason"},
            )
        filing.rejection_reason = rejection_reason

    if target == FilingStatus.DRAFT and previous in (
        FilingStatus.IN_REVIEW,
        FilingStatus.APPROVED,
    ):
        # Sent back for changes: the previous approval no longer stands, or a
        # stale ``reviewed_by`` would make an unreviewed draft look reviewed.
        filing.reviewed_by_id = None
        filing.reviewed_at = None

    if notes:
        filing.notes = notes

    satisfied = _sync_deadline(db, filing)
    db.flush()

    logger.info(
        "Filing %s moved %s -> %s by user %s", filing.id, previous, target, ctx.user_id
    )
    return TransitionOutcome(
        filing_id=filing.id,
        previous=previous,
        current=target,
        was_late=was_late,
        deadline_satisfied=satisfied,
    )


def _sync_deadline(db: Session, filing: Filing) -> bool:
    """Mark the filing's deadline satisfied, or un-satisfy a reopened one.

    Both directions matter. Forward is obvious. Backward is what stops a filing
    that was submitted, rejected by the portal and sent back to draft from
    sitting silently past its due date because the reminder was switched off
    three weeks earlier.
    """
    deadline = db.execute(
        select(Deadline).where(
            Deadline.filing_id == filing.id, Deadline.deleted_at.is_(None)
        )
    ).scalar_one_or_none()
    if deadline is None:
        return False

    should_be_satisfied = filing.status in _SATISFYING
    if should_be_satisfied and not deadline.is_satisfied:
        deadline.is_satisfied = True
        deadline.satisfied_at = utcnow()
        return True
    if not should_be_satisfied and deadline.is_satisfied:
        deadline.is_satisfied = False
        deadline.satisfied_at = None
        # Reminders already sent are cleared so the reopened filing is chased
        # again from scratch. Keeping them would mean a filing reopened five
        # days before the deadline never gets another reminder, because the
        # 30-, 15- and 7-day ones are all recorded as sent.
        deadline.reminders_sent_json = []
        deadline.escalation_level = 0
    return False


def ensure_deadline(db: Session, filing: Filing) -> Deadline:
    """The filing's deadline row, created if absent and kept in step with it.

    Called from both the generator and the manual-create route so that a filing
    made by hand is chased exactly like a generated one — the alternative is a
    class of filing that silently never reminds anybody.
    """
    deadline = db.execute(
        select(Deadline).where(
            Deadline.filing_id == filing.id, Deadline.deleted_at.is_(None)
        )
    ).scalar_one_or_none()

    if deadline is None:
        deadline = Deadline(
            organization_id=filing.organization_id,
            filing_id=filing.id,
            due_date=filing.effective_due_date,
            is_satisfied=filing.status in _SATISFYING,
            reminders_sent_json=[],
        )
        db.add(deadline)
        db.flush()
        return deadline

    # An extension moves the deadline. The reminder history is *not* cleared:
    # a 30-day reminder that went out against the old date was still sent, and
    # re-sending it would train people to ignore the channel.
    if deadline.due_date != filing.effective_due_date:
        deadline.due_date = filing.effective_due_date
    return deadline
