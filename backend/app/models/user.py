"""Users, their role, and the CA-firm staff → client assignments."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import UserRole
from app.models.mixins import (
    OrgScopedMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    live_unique,
)

if TYPE_CHECKING:
    from app.models.organization import Organization


class User(Base, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    """A person who signs in.

    A user belongs to exactly one organization — their employer. A CA firm's
    staff belong to the firm, not to the clients they work on; access to a
    client is granted by :class:`ClientAssignment` and mediated by the
    ``client_org_id`` token claim described in section 6.1.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)

    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, native_enum=False, length=32), default=UserRole.STAFF, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- TOTP (section 8.2) ----------------------------------------------
    # The secret is stored encrypted, like the statutory identifiers are.
    # ``totp_confirmed_at`` is what gates login: a secret that has been issued
    # but never proved by a valid code must not lock anyone out, and must not
    # count as second-factor coverage either.
    totp_secret: Mapped[str | None] = mapped_column(String(255))
    totp_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Set on a failed login and cleared on a successful one. Used for the
    # lockout window, so that a stolen password list cannot be walked through
    # the login endpoint at full speed.
    failed_login_count: Mapped[int] = mapped_column(default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)

    organization: Mapped["Organization"] = relationship(  # noqa: F821,UP037
        back_populates="users"
    )

    __table_args__ = (
        # Email is unique per tenant rather than globally: the same accountant
        # can legitimately hold an account at their firm and at a company they
        # are a director of. Scoped on ``deleted_at`` so a removed user's
        # address can be re-issued.
        live_unique("uq_users_org_email", "organization_id", "email"),
        Index("ix_users_org_created", "organization_id", "created_at"),
        Index("ix_users_org_role", "organization_id", "role"),
    )

    @property
    def has_totp(self) -> bool:
        """Whether a *confirmed* second factor is on the account."""
        return self.totp_secret is not None and self.totp_confirmed_at is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User id={self.id} email={self.email!r} role={self.role}>"


class ClientAssignment(Base, TimestampMixin, SoftDeleteMixin):
    """Which CA-firm staff may act for which client organization.

    Section 4.5: "Client-level access controls ensure staff can only see
    assigned clients." Admins and Compliance Managers of a firm see every
    client of that firm without a row here; Staff and Read-Only users see only
    what this table grants them. Keeping the grant explicit means widening
    someone's access is a recorded, auditable write rather than a role change
    that quietly opens everything.
    """

    __tablename__ = "client_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalised from ``clients`` so the hot path — "may this user read org
    # X?" — is one index lookup rather than a join on every request.
    client_org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Caps what the assignment grants, independently of the user's own role: a
    # Compliance Manager can be given read-only sight of a client they are not
    # working on. Null means "whatever the user's own role allows".
    granted_role: Mapped[UserRole | None] = mapped_column(
        Enum(UserRole, native_enum=False, length=32)
    )

    __table_args__ = (
        live_unique("uq_assignment_user_client", "user_id", "client_id"),
        Index("ix_client_assignments_user_org", "user_id", "client_org_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ClientAssignment user={self.user_id} client_org={self.client_org_id}>"
