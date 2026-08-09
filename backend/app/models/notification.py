"""Notification records and per-organization channel preferences."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.enums import NotificationChannel, NotificationStatus
from app.models.mixins import (
    JSONType,
    OrgScopedMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    live_unique,
)


class Notification(Base, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    """One message CompliPilot sent, or tried to.

    Written before the send is attempted, not after. A row that stays
    ``PENDING`` is a send that crashed mid-flight, and that is a state worth
    being able to see; a table written only on success cannot distinguish
    "never attempted" from "attempted and lost".
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)

    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(NotificationChannel, native_enum=False, length=32), nullable=False, index=True
    )
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    subject: Mapped[str | None] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, native_enum=False, length=32),
        default=NotificationStatus.PENDING,
        nullable=False,
        index=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))

    # What prompted it: "deadline_reminder", "regulatory_alert",
    # "breach_notification". Plus the entity, so the UI can link back.
    kind: Mapped[str] = mapped_column(String(64), nullable=False, default="general", index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))

    # Set for reminders: which offset this one was. Together with the entity
    # it makes a resend detectable.
    reminder_offset_days: Mapped[int | None] = mapped_column()

    metadata_json: Mapped[dict | None] = mapped_column(JSONType)

    __table_args__ = (
        Index("ix_notifications_org_status", "organization_id", "status"),
        Index("ix_notifications_entity", "entity_type", "entity_id"),
        Index("ix_notifications_org_created", "organization_id", "created_at"),
        # The retry worker's queue: pending or failed, oldest first.
        Index("ix_notifications_retry", "status", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Notification id={self.id} {self.channel} {self.status}>"


class NotificationPreference(Base, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    """How one organization wants to be reached.

    Per organization rather than per user because compliance alerts are
    addressed to the entity: a deadline belongs to the company, and the people
    who should hear about it are whoever currently holds the compliance role.
    Individual opt-outs are the ``user_id`` rows.
    """

    __tablename__ = "notification_preferences"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Null means the organization-wide default; a value means this user's
    # personal override.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sms_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    in_app_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    email_address: Mapped[str | None] = mapped_column(String(255))
    whatsapp_number: Mapped[str | None] = mapped_column(String(32))
    sms_number: Mapped[str | None] = mapped_column(String(32))

    # Overrides ``settings.reminder_offsets`` for this organization. Null means
    # take the deployment default, so a tenant who never expressed a preference
    # follows the product's rather than being frozen at whatever the default
    # was on the day they signed up.
    reminder_offsets_json: Mapped[list | None] = mapped_column(JSONType)

    # Local quiet hours, 0-23. A compliance reminder at 03:00 IST trains people
    # to mute the channel, which costs more than the missed hour gains.
    quiet_hours_start: Mapped[int | None] = mapped_column()
    quiet_hours_end: Mapped[int | None] = mapped_column()

    __table_args__ = (
        live_unique("uq_notif_pref_org_user", "organization_id", "user_id"),
        Index("ix_notification_preferences_org_created", "organization_id", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<NotificationPreference org={self.organization_id} user={self.user_id}>"
