"""Composing, recording and delivering notifications (section 4.1).

Every message the product sends passes through here, and the shape of the
module follows from three decisions:

**The row is written before the send is attempted.** :func:`queue` inserts a
``PENDING`` notification and returns it; :func:`deliver` then tries the channel
and moves it to ``SENT``, ``FAILED`` or ``SKIPPED``. A table written only after
a successful send cannot tell "never attempted" from "attempted and lost", and
the second is the case anyone actually needs to investigate.

**A missing credential is not a failure.** A deployment with no WhatsApp token
records ``SKIPPED`` and moves on. Distinguishing it from ``FAILED`` is what
keeps the retry worker from re-attempting, every twenty minutes forever,
something that cannot succeed until someone changes the configuration.

**Delivery never raises into its caller.** The callers are a nightly sweep and
a request handler that has already done the useful work. A reminder that could
not be sent must not roll back the filing it was reminding about, so transport
errors are caught, recorded on the row, and returned as a value.

Channel transports live in ``_send_*`` functions with one signature, so adding
SMS later is a function and a dict entry rather than a change to the dispatch.
"""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.enums import NotificationChannel, NotificationStatus, UserRole
from app.models.mixins import utcnow
from app.models.notification import Notification, NotificationPreference
from app.models.user import User

logger = logging.getLogger(__name__)

# Indian statutory deadlines are IST dates, and quiet hours are what a person
# in India considers night. Both are meaningless in UTC.
IST = ZoneInfo("Asia/Kolkata")

# Give up on a notification after this many tries. Five spreads over roughly
# 100 minutes at the retry worker's 20-minute cadence, which covers a provider
# blip without keeping a permanently bad address in the queue forever.
MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class Recipient:
    """One address on one channel, and the user it belongs to if any."""

    channel: NotificationChannel
    address: str
    user_id: int | None = None


@dataclass(frozen=True)
class DeliveryResult:
    """The outcome of one send attempt."""

    status: NotificationStatus
    provider_message_id: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == NotificationStatus.SENT


# --------------------------------------------------------------------------
# Preferences
# --------------------------------------------------------------------------


def preferences_for(
    db: Session, organization_id: int, *, user_id: int | None = None
) -> NotificationPreference | None:
    """The preference row that governs, falling back to the org-wide default.

    A user's personal row wins over their organization's. Absent both, the
    caller takes the deployment defaults — which is deliberate: a tenant who
    never expressed a preference should follow the product's current default
    rather than be frozen at whatever it was the day they signed up.
    """
    if user_id is not None:
        personal = db.execute(
            select(NotificationPreference).where(
                NotificationPreference.organization_id == organization_id,
                NotificationPreference.user_id == user_id,
                NotificationPreference.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if personal is not None:
            return personal

    return db.execute(
        select(NotificationPreference).where(
            NotificationPreference.organization_id == organization_id,
            NotificationPreference.user_id.is_(None),
            NotificationPreference.deleted_at.is_(None),
        )
    ).scalar_one_or_none()


def reminder_offsets_for(
    db: Session, organization_id: int, *, user_id: int | None = None
) -> list[int]:
    """The reminder schedule for an organization, descending.

    Descending because the sweep walks from the furthest-out offset to the
    nearest and takes the first that is due and unsent; ascending order would
    fire the 1-day reminder thirty days early.
    """
    pref = preferences_for(db, organization_id, user_id=user_id)
    configured = pref.reminder_offsets_json if pref else None
    if not configured:
        return settings.reminder_offsets

    offsets = {int(v) for v in configured if isinstance(v, (int, float)) and int(v) >= 0}
    return sorted(offsets, reverse=True) or settings.reminder_offsets


def in_quiet_hours(pref: NotificationPreference | None, *, now: datetime | None = None) -> bool:
    """Whether *now* falls inside the organization's quiet hours, in IST.

    Handles the window that wraps midnight — 22 to 7 is the common setting and
    the naive ``start <= hour < end`` comparison reads it as empty, which would
    silently disable quiet hours for everyone who set a sensible value.
    """
    if pref is None or pref.quiet_hours_start is None or pref.quiet_hours_end is None:
        return False

    start, end = pref.quiet_hours_start, pref.quiet_hours_end
    if start == end:
        # A zero-width window. Read as "no quiet hours" rather than "always",
        # because the alternative silently stops every notification.
        return False

    hour = (now or utcnow()).astimezone(IST).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _channel_enabled(pref: NotificationPreference | None, channel: NotificationChannel) -> bool:
    """Whether a channel is switched on. Absent preferences take the model default."""
    if pref is None:
        # Matches the column defaults on NotificationPreference: everything but
        # SMS, which costs money per message and is opt-in.
        return channel != NotificationChannel.SMS
    return {
        NotificationChannel.EMAIL: pref.email_enabled,
        NotificationChannel.WHATSAPP: pref.whatsapp_enabled,
        NotificationChannel.SMS: pref.sms_enabled,
        NotificationChannel.IN_APP: pref.in_app_enabled,
    }[channel]


def _preferred_address(
    pref: NotificationPreference | None, channel: NotificationChannel, user: User | None
) -> str | None:
    """Where to send, preferring the explicit preference over the user record."""
    if pref is not None:
        override = {
            NotificationChannel.EMAIL: pref.email_address,
            NotificationChannel.WHATSAPP: pref.whatsapp_number,
            NotificationChannel.SMS: pref.sms_number,
            NotificationChannel.IN_APP: None,
        }[channel]
        if override:
            return override

    if user is None:
        return None
    if channel in (NotificationChannel.WHATSAPP, NotificationChannel.SMS):
        return user.phone
    # Email, and in-app — which has no transport address, so the email stands
    # in as a stable label that makes the notification history readable.
    return user.email


def _resolve(
    db: Session,
    organization_id: int,
    roles: set[UserRole] | None,
    wanted: set[NotificationChannel],
) -> list[Recipient]:
    """Users in *roles*, expanded to one recipient per enabled channel."""
    stmt = select(User).where(
        User.organization_id == organization_id,
        User.deleted_at.is_(None),
        User.is_active.is_(True),
    )
    if roles:
        stmt = stmt.where(User.role.in_(list(roles)))

    out: list[Recipient] = []
    seen: set[tuple[str, str]] = set()

    for user in db.execute(stmt.order_by(User.id)).scalars().all():
        pref = preferences_for(db, organization_id, user_id=user.id)
        for channel in wanted:
            if not _channel_enabled(pref, channel):
                continue
            address = _preferred_address(pref, channel, user)
            if not address:
                continue
            # Two users sharing an address (a shared compliance@ mailbox) get
            # one message, not two identical ones.
            key = (str(channel), address.strip().lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(Recipient(channel=channel, address=address, user_id=user.id))
    return out


def recipients_for(
    db: Session,
    organization_id: int,
    *,
    roles: set[UserRole] | None = None,
    channels: set[NotificationChannel] | None = None,
    fallback_roles: set[UserRole] | None = None,
) -> list[Recipient]:
    """Who to tell about something that belongs to an organization.

    Compliance alerts are addressed to the *entity*, not to an individual — a
    deadline belongs to the company and the people who should hear about it are
    whoever currently holds the compliance role. So this resolves roles to the
    users holding them now, rather than notifying a subscriber list that goes
    stale the first time someone leaves.

    Each user's own preference row governs their channels, so one person opting
    out of WhatsApp does not opt out their colleagues.

    **``fallback_roles`` is what stops an organization being unreachable.** The
    escalation ladder addresses ordinary reminders to the people who do the
    work — Staff and Compliance Manager — and widens to the Admin only once a
    filing is overdue. In a one-person practice, where the sole user is the
    Admin, that reasoning resolves to nobody at all, and the organization would
    receive no deadline reminders whatsoever while every sweep recorded them as
    sent. The fallback is consulted only when the primary set is empty, so it
    cannot widen an audience that already has someone in it.
    """
    wanted = channels or {NotificationChannel.EMAIL, NotificationChannel.IN_APP}

    primary = _resolve(db, organization_id, roles, wanted)
    if primary or not fallback_roles or not roles:
        return primary

    fallback = _resolve(db, organization_id, fallback_roles, wanted)
    if fallback:
        logger.info(
            "No user in %s for organization %s; falling back to %s",
            sorted(str(r) for r in roles),
            organization_id,
            sorted(str(r) for r in fallback_roles),
        )
    return fallback


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------


def _send_email(notification: Notification) -> DeliveryResult:
    """Deliver over SMTP.

    No configured host means the deployment has no mail transport — SKIPPED,
    not FAILED, so the retry worker leaves it alone.
    """
    if not settings.smtp_host:
        return DeliveryResult(NotificationStatus.SKIPPED, error="SMTP is not configured")

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = notification.recipient
    message["Subject"] = notification.subject or "CompliPilot notification"
    message.set_content(notification.content)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)

    # SMTP gives no per-message id that survives the connection; the envelope
    # recipient is the only handle worth recording.
    return DeliveryResult(NotificationStatus.SENT)


def _send_whatsapp(notification: Notification) -> DeliveryResult:
    """Deliver over the WhatsApp Cloud API."""
    if not (settings.whatsapp_access_token and settings.whatsapp_phone_number_id):
        return DeliveryResult(
            NotificationStatus.SKIPPED, error="WhatsApp is not configured"
        )

    url = f"{settings.whatsapp_api_url}/{settings.whatsapp_phone_number_id}/messages"
    response = httpx.post(
        url,
        headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
        json={
            "messaging_product": "whatsapp",
            "to": notification.recipient,
            "type": "text",
            "text": {"body": notification.content},
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    messages = payload.get("messages") or [{}]
    return DeliveryResult(
        NotificationStatus.SENT, provider_message_id=messages[0].get("id")
    )


def _send_sms(notification: Notification) -> DeliveryResult:
    """SMS has no configured provider yet.

    Recorded rather than silently dropped: the row in the history is what makes
    "we never sent SMS" visible instead of looking like a delivery that worked.
    """
    return DeliveryResult(NotificationStatus.SKIPPED, error="No SMS provider configured")


def _send_in_app(notification: Notification) -> DeliveryResult:
    """In-app delivery is the row itself; there is nothing to transmit."""
    return DeliveryResult(NotificationStatus.SENT)


_TRANSPORTS = {
    NotificationChannel.EMAIL: _send_email,
    NotificationChannel.WHATSAPP: _send_whatsapp,
    NotificationChannel.SMS: _send_sms,
    NotificationChannel.IN_APP: _send_in_app,
}


# --------------------------------------------------------------------------
# Queue and deliver
# --------------------------------------------------------------------------


def queue(
    db: Session,
    *,
    organization_id: int,
    channel: NotificationChannel,
    recipient: str,
    content: str,
    subject: str | None = None,
    kind: str = "general",
    user_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    reminder_offset_days: int | None = None,
    metadata: dict | None = None,
) -> Notification:
    """Record a notification as PENDING. Flushes, does not commit."""
    notification = Notification(
        organization_id=organization_id,
        channel=channel,
        recipient=recipient,
        user_id=user_id,
        subject=subject,
        content=content,
        status=NotificationStatus.PENDING,
        kind=kind,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        reminder_offset_days=reminder_offset_days,
        metadata_json=metadata,
    )
    db.add(notification)
    db.flush()
    return notification


def deliver(db: Session, notification: Notification) -> DeliveryResult:
    """Attempt one send and record the outcome on the row.

    Returns the result rather than raising. Callers are sweeps and request
    handlers that have already done the work the notification is about; a
    transport error must not undo it.
    """
    notification.attempts += 1
    transport = _TRANSPORTS[notification.channel]

    try:
        result = transport(notification)
    except Exception as exc:  # noqa: BLE001 - the whole point is not to propagate
        logger.warning(
            "Notification %s failed on %s: %s", notification.id, notification.channel, exc
        )
        result = DeliveryResult(NotificationStatus.FAILED, error=str(exc)[:2000])

    notification.status = result.status
    notification.error = result.error
    if result.provider_message_id:
        notification.provider_message_id = result.provider_message_id
    if result.ok:
        notification.sent_at = utcnow()
    db.flush()
    return result


def send(
    db: Session,
    *,
    organization_id: int,
    recipient: Recipient,
    content: str,
    subject: str | None = None,
    kind: str = "general",
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    reminder_offset_days: int | None = None,
    metadata: dict | None = None,
) -> Notification:
    """Queue and immediately attempt one notification."""
    notification = queue(
        db,
        organization_id=organization_id,
        channel=recipient.channel,
        recipient=recipient.address,
        content=content,
        subject=subject,
        kind=kind,
        user_id=recipient.user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        reminder_offset_days=reminder_offset_days,
        metadata=metadata,
    )
    deliver(db, notification)
    return notification


def already_notified(
    db: Session,
    *,
    organization_id: int,
    kind: str,
    entity_type: str,
    entity_id: str | int,
    reminder_offset_days: int | None = None,
) -> bool:
    """Whether this exact message already went out.

    The second half of the reminder idempotency guarantee. ``Deadline`` records
    which offsets it has sent, which is the fast path; this is the check that
    still holds when a deadline row was rebuilt, or for the notifications that
    have no deadline behind them at all — a breach clock, an escalation.

    ``FAILED`` deliberately does not count as notified, so a send that failed is
    retried rather than suppressed forever by its own failure.
    """
    stmt = select(Notification.id).where(
        Notification.organization_id == organization_id,
        Notification.kind == kind,
        Notification.entity_type == entity_type,
        Notification.entity_id == str(entity_id),
        Notification.status.in_(
            [NotificationStatus.SENT, NotificationStatus.PENDING, NotificationStatus.SKIPPED]
        ),
        Notification.deleted_at.is_(None),
    )
    if reminder_offset_days is not None:
        stmt = stmt.where(Notification.reminder_offset_days == reminder_offset_days)
    return db.execute(stmt.limit(1)).first() is not None


# --------------------------------------------------------------------------
# Message composition
#
# Kept here rather than in the tasks so the wording of a reminder is in one
# place, and so a test can assert on it without running a sweep.
# --------------------------------------------------------------------------


def _rupees(paise: int | None) -> str:
    if not paise:
        return ""
    return f"₹{paise / 100:,.2f}"


def deadline_reminder_message(
    filing, *, days_until_due: int, organization_name: str
) -> tuple[str, str]:
    """``(subject, body)`` for a filing reminder.

    Reads the *effective* due date, so a filing whose deadline the regulator
    extended is described by the date that now binds rather than the original.
    """
    form = filing.filing_type or str(filing.regulation).upper()
    due = filing.effective_due_date

    if days_until_due < 0:
        urgency = f"OVERDUE by {abs(days_until_due)} day(s)"
    elif days_until_due == 0:
        urgency = "DUE TODAY"
    else:
        urgency = f"due in {days_until_due} day(s)"

    subject = f"[CompliPilot] {form} for {filing.period_key} is {urgency}"
    lines = [
        f"{organization_name}",
        "",
        f"{form} for period {filing.period_key} is {urgency}.",
        f"Due date: {due.isoformat()}",
        f"Status: {filing.status}",
    ]
    if filing.extended_due_date:
        lines.append(f"(Original due date was {filing.due_date.isoformat()}, since extended.)")
    if filing.tax_payable_paise:
        lines.append(f"Tax payable: {_rupees(filing.tax_payable_paise)}")
    lines += ["", "Open CompliPilot to prepare, review and file this return."]
    return subject, "\n".join(lines)


def escalation_message(
    filing, *, days_overdue: int, level: int, organization_name: str
) -> tuple[str, str]:
    """``(subject, body)`` for an overdue filing that has been escalated."""
    form = filing.filing_type or str(filing.regulation).upper()
    subject = (
        f"[CompliPilot] ESCALATION L{level}: {form} for {filing.period_key} "
        f"is {days_overdue} day(s) overdue"
    )
    body = "\n".join(
        [
            f"{organization_name}",
            "",
            f"{form} for period {filing.period_key} was due on "
            f"{filing.effective_due_date.isoformat()} and remains {filing.status}.",
            f"It is now {days_overdue} day(s) overdue and has been escalated to level {level}.",
            "",
            "Late filing accrues interest and late fees from the due date. "
            "This filing needs attention today.",
        ]
    )
    return subject, body


def breach_deadline_message(
    breach, *, hours_remaining: float, organization_name: str
) -> tuple[str, str]:
    """``(subject, body)`` for the DPDP 72-hour breach-notification clock."""
    if hours_remaining < 0:
        window = f"PASSED {abs(hours_remaining):.0f} hour(s) ago"
    else:
        window = f"expires in {hours_remaining:.0f} hour(s)"

    subject = f"[CompliPilot] DPDP breach notification window {window}"
    body = "\n".join(
        [
            f"{organization_name}",
            "",
            f"Personal data breach #{breach.id} was detected on "
            f"{breach.detected_at.date().isoformat()}.",
            "",
            f"The DPDP Act requires notification to the Data Protection Board "
            f"and to affected Data Principals within 72 hours. That window {window}.",
            "",
            "Record the notification in CompliPilot once it has been made.",
        ]
    )
    return subject, body


def today_ist() -> date:
    """Today's date in IST.

    Every deadline comparison uses this rather than ``date.today()``: a sweep
    running on a UTC server just after 18:30 UTC is already tomorrow in India,
    and comparing an IST statutory deadline against a UTC date sends "1 day
    left" reminders on the wrong day around month boundaries.
    """
    return utcnow().astimezone(IST).date()
