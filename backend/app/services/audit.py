"""Writing and verifying the tamper-evident audit chain (section 4.7).

The only writer of :class:`~app.models.audit.AuditTrail`. Everything else calls
:func:`record`.

**How the chain works.** Each entry's ``checksum`` is a SHA-256 over a
canonical serialization of the entry's own fields *concatenated with the
previous entry's checksum*. Changing any field of entry N changes its checksum,
which was an input to entry N+1's, and so on — so a single edit invalidates
every entry after it, and :func:`verify_chain` reports the first position where
the recomputed value stops matching.

**What that does and does not prove.** It proves that history has not been
edited *in place*. It does not stop someone with write access from truncating
the tail and continuing from there — no in-database scheme can, because the
attacker holds the same key material the writer does. Truncation is what
:func:`chain_head` is for: exporting the head checksum somewhere the database
cannot reach turns "the chain is internally consistent" into "the chain matches
what we published on the 14th".
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.tenancy import TenantContext
from app.models.audit import GENESIS_CHECKSUM, AuditTrail
from app.models.enums import AuditAction
from app.models.mixins import utcnow

logger = logging.getLogger(__name__)

# Fields that must never reach the trail even if a caller passes them in a
# before/after payload. The trail is read by more people than the table it
# describes — that is its purpose — so a password hash or a TOTP secret copied
# into it widens exposure rather than recording it.
REDACTED_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "totp_secret",
        "access_token",
        "refresh_token",
        "authorization",
        "api_key",
        "openrouter_api_key",
        "whatsapp_access_token",
        "smtp_password",
    }
)

_REDACTED = "[redacted]"

# How many attempts to claim the next sequence number before giving up. Two
# concurrent writers for one organization race on the unique constraint; the
# loser retries with a re-read tail. Three is generous — the window is a single
# INSERT — and bounded so a genuinely broken chain does not spin.
_MAX_SEQUENCE_ATTEMPTS = 3


def _redact(value: Any) -> Any:
    """Recursively strip secret-looking keys from a payload."""
    if isinstance(value, dict):
        return {
            k: (_REDACTED if k.lower() in REDACTED_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _canonical(value: Any) -> str:
    """A stable string for hashing.

    ``sort_keys`` because a dict's insertion order is not part of its meaning,
    and a checksum that changed when a caller happened to build the payload in
    a different order would fail verification on data nobody touched.
    ``default=str`` so a date or a Decimal in a payload hashes rather than
    raising — a logging path must not be the thing that breaks a write.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _canonical_timestamp(value: datetime) -> str:
    """A single spelling of an instant, for hashing.

    The checksum must depend on *when the thing happened*, not on how a driver
    chose to render it. ``timestamp.isoformat()`` does not have that property:
    what goes into the database as an aware UTC datetime comes back naive from
    SQLite, and comes back from Postgres rendered in the session's timezone —
    so the same row hashed to one value on write and a different one on
    verification, and an untampered chain failed its own audit.

    Normalising to UTC and fixing the precision makes the input depend on the
    instant alone. A naive value is read as UTC because that is the only thing
    this application ever stores; guessing local time would silently shift
    every historical entry the first time a server's timezone changed.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def compute_checksum(
    *,
    organization_id: int,
    sequence: int,
    user_id: int | None,
    actor_label: str,
    action: str,
    entity_type: str,
    entity_id: str | None,
    timestamp: datetime,
    ip_address: str | None,
    before: dict | None,
    after: dict | None,
    summary: str | None,
    prev_checksum: str,
) -> str:
    """The SHA-256 for one entry.

    Every field that a reader would rely on is an input. ``user_id`` and
    ``ip_address`` in particular: an entry whose actor could be rewritten
    without breaking the chain would make the trail useless for the one
    question it exists to answer.

    Fields are joined with a delimiter that cannot occur in the JSON encoding
    of the parts, so ``("ab", "c")`` and ``("a", "bc")`` cannot hash alike.
    """
    parts = [
        str(organization_id),
        str(sequence),
        str(user_id or ""),
        actor_label,
        str(action),
        entity_type,
        entity_id or "",
        _canonical_timestamp(timestamp),
        ip_address or "",
        _canonical(before) if before is not None else "",
        _canonical(after) if after is not None else "",
        summary or "",
        prev_checksum,
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _tail(db: Session, organization_id: int) -> tuple[int, str]:
    """``(last_sequence, last_checksum)`` for an organization's chain."""
    row = db.execute(
        select(AuditTrail.sequence, AuditTrail.checksum)
        .where(AuditTrail.organization_id == organization_id)
        .order_by(AuditTrail.sequence.desc())
        .limit(1)
    ).first()
    if row is None:
        return 0, GENESIS_CHECKSUM
    return int(row[0]), str(row[1])


def record(
    db: Session,
    *,
    organization_id: int,
    action: AuditAction | str,
    entity_type: str,
    entity_id: Any = None,
    user_id: int | None = None,
    actor_label: str = "system",
    ip_address: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    summary: str | None = None,
) -> AuditTrail:
    """Append one entry to an organization's chain.

    Flushes but does not commit: the entry joins the caller's transaction, so
    an action that is rolled back does not leave an audit entry claiming it
    happened. That coupling is deliberate and is the reason this takes the
    caller's session rather than opening its own.
    """
    before = _redact(before) if before is not None else None
    after = _redact(after) if after is not None else None
    timestamp = utcnow()

    last_error: IntegrityError | None = None
    for _ in range(_MAX_SEQUENCE_ATTEMPTS):
        sequence, prev_checksum = _tail(db, organization_id)
        sequence += 1

        entry = AuditTrail(
            organization_id=organization_id,
            sequence=sequence,
            user_id=user_id,
            actor_label=actor_label,
            action=AuditAction(action) if not isinstance(action, AuditAction) else action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            timestamp=timestamp,
            ip_address=ip_address,
            user_agent=(user_agent or None) and str(user_agent)[:512],
            request_id=request_id,
            before_json=before,
            after_json=after,
            summary=summary,
            prev_checksum=prev_checksum,
            checksum=compute_checksum(
                organization_id=organization_id,
                sequence=sequence,
                user_id=user_id,
                actor_label=actor_label,
                action=str(action),
                entity_type=entity_type,
                entity_id=str(entity_id) if entity_id is not None else None,
                timestamp=timestamp,
                ip_address=ip_address,
                before=before,
                after=after,
                summary=summary,
                prev_checksum=prev_checksum,
            ),
        )
        db.add(entry)
        try:
            # A nested transaction, so losing the race rolls back only this
            # INSERT. Without it the failed flush would poison the caller's
            # whole transaction and take the business write down with it.
            with db.begin_nested():
                db.flush()
            return entry
        except IntegrityError as exc:
            last_error = exc
            db.expunge(entry)
            continue

    # Three collisions in a row is not contention, it is a broken chain.
    raise RuntimeError(
        f"Could not append audit entry for organization {organization_id}"
    ) from last_error


def record_for(
    db: Session,
    ctx: TenantContext,
    *,
    action: AuditAction | str,
    entity_type: str,
    entity_id: Any = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    summary: str | None = None,
) -> list[AuditTrail]:
    """Record an action, in both chains when a CA firm acted for a client.

    Returns the entries written — one normally, two for a delegated action.
    Writing to both is what makes the trail complete from either side: the
    client's history has to show that something happened to their filing, and
    the firm's has to show that their staff member did it. Neither alone
    answers a regulator asking the other question.
    """
    actor = ctx.user.full_name or ctx.user.email
    common = {
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "user_id": ctx.user_id,
        "actor_label": actor,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "request_id": request_id,
        "before": before,
        "after": after,
    }

    entries = [
        record(db, organization_id=ctx.org_id, summary=summary, **common)  # type: ignore[arg-type]
    ]

    if ctx.is_delegated and ctx.home_org_id != ctx.org_id:
        firm_summary = summary or f"{action} on {entity_type}"
        entries.append(
            record(
                db,
                organization_id=ctx.home_org_id,
                summary=f"{firm_summary} (for client organization {ctx.org_id})",
                **common,  # type: ignore[arg-type]
            )
        )

    return entries


@dataclass(frozen=True)
class ChainVerification:
    """The result of walking one organization's chain."""

    organization_id: int
    entries_checked: int
    is_valid: bool
    # 1-based position of the first bad entry, if any.
    broken_at_sequence: int | None = None
    broken_entry_id: int | None = None
    reason: str | None = None
    head_checksum: str | None = None

    def as_dict(self) -> dict:
        return {
            "organization_id": self.organization_id,
            "entries_checked": self.entries_checked,
            "is_valid": self.is_valid,
            "broken_at_sequence": self.broken_at_sequence,
            "broken_entry_id": self.broken_entry_id,
            "reason": self.reason,
            "head_checksum": self.head_checksum,
        }


def verify_chain(
    db: Session, organization_id: int, *, batch_size: int = 1000
) -> ChainVerification:
    """Recompute every checksum in an organization's chain, in order.

    Streams in batches rather than loading the trail into memory: this table is
    the one that grows without bound, and a CA firm four years in will have
    millions of entries.

    Stops at the first break. Everything after a break is unverifiable anyway —
    it chains off a value that is already wrong — so continuing would report a
    million failures for one tamper.
    """
    expected_prev = GENESIS_CHECKSUM
    expected_sequence = 1
    checked = 0
    head: str | None = None
    offset = 0

    while True:
        batch = (
            db.execute(
                select(AuditTrail)
                .where(AuditTrail.organization_id == organization_id)
                .order_by(AuditTrail.sequence)
                .offset(offset)
                .limit(batch_size)
            )
            .scalars()
            .all()
        )
        if not batch:
            break
        offset += len(batch)

        for entry in batch:
            checked += 1

            if entry.sequence != expected_sequence:
                return ChainVerification(
                    organization_id,
                    checked,
                    False,
                    entry.sequence,
                    entry.id,
                    f"Sequence gap: expected {expected_sequence}, found {entry.sequence}",
                    head,
                )

            if entry.prev_checksum != expected_prev:
                return ChainVerification(
                    organization_id,
                    checked,
                    False,
                    entry.sequence,
                    entry.id,
                    "Previous-checksum link does not match the preceding entry",
                    head,
                )

            recomputed = compute_checksum(
                organization_id=entry.organization_id,
                sequence=entry.sequence,
                user_id=entry.user_id,
                actor_label=entry.actor_label,
                action=str(entry.action),
                entity_type=entry.entity_type,
                entity_id=entry.entity_id,
                timestamp=entry.timestamp,
                ip_address=entry.ip_address,
                before=entry.before_json,
                after=entry.after_json,
                summary=entry.summary,
                prev_checksum=entry.prev_checksum,
            )
            if recomputed != entry.checksum:
                return ChainVerification(
                    organization_id,
                    checked,
                    False,
                    entry.sequence,
                    entry.id,
                    "Entry content does not match its stored checksum",
                    head,
                )

            expected_prev = entry.checksum
            expected_sequence += 1
            head = entry.checksum

    return ChainVerification(organization_id, checked, True, head_checksum=head)


def chain_head(db: Session, organization_id: int) -> dict:
    """The current head of a chain — its length and last checksum.

    Meant to be published somewhere outside the database (an email to the
    compliance officer, a notary, a commit). A stored head is what closes the
    truncation gap described at the top of this module.
    """
    sequence, checksum = _tail(db, organization_id)
    total = db.execute(
        select(func.count())
        .select_from(AuditTrail)
        .where(AuditTrail.organization_id == organization_id)
    ).scalar_one()
    return {
        "organization_id": organization_id,
        "sequence": sequence,
        "checksum": checksum,
        "entry_count": int(total),
        "as_of": utcnow().isoformat(),
    }
