"""Organizations — the tenant root, for both CA firms and the companies they act for."""
from __future__ import annotations

from datetime import date

from sqlalchemy import (
    Boolean,
    Date,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import EntityType, OrgType, PlanTier
from app.models.mixins import JSONType, Paise, SoftDeleteMixin, TimestampMixin, live_unique


class Organization(Base, TimestampMixin, SoftDeleteMixin):
    """A CA firm or a client company.

    Both live in one table because the hierarchy is a relationship between
    organizations, not two different kinds of thing: a CA firm has users,
    settings, documents and an audit trail exactly as a company does, and a
    mid-size enterprise with an in-house compliance team is a ``COMPANY`` that
    happens to have no CA firm above it.

    This table does *not* carry ``OrgScopedMixin``. It is the tenant; the
    ``id`` here is the ``organization_id`` everywhere else.
    """

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(255))
    type: Mapped[OrgType] = mapped_column(
        Enum(OrgType, native_enum=False, length=32), nullable=False
    )
    entity_type: Mapped[EntityType | None] = mapped_column(
        Enum(EntityType, native_enum=False, length=32)
    )

    # --- Statutory identifiers -------------------------------------------
    # Nullable because a brand-new proprietorship may hold only a PAN, and a
    # CA firm onboarding a client often has one number before the rest. Format
    # is validated in the schema layer, not here, so an import of historical
    # data can be corrected rather than rejected outright.
    #
    # Section 8.1 requires these be encrypted at the column level. That is done
    # by the application through :mod:`app.core.crypto` — the column stores
    # ciphertext, and the ``*_fingerprint`` column beside it stores a keyed
    # hash so uniqueness and lookup still work without decrypting the table.
    gstin: Mapped[str | None] = mapped_column(String(255))
    gstin_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    pan: Mapped[str | None] = mapped_column(String(255))
    pan_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    cin: Mapped[str | None] = mapped_column(String(255))
    cin_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    # LLP identification number, and the ICAI firm registration number for a
    # CA firm. Neither is PII in the DPDP sense, so both are stored in clear.
    llpin: Mapped[str | None] = mapped_column(String(32))
    firm_registration_no: Mapped[str | None] = mapped_column(String(32))
    tan: Mapped[str | None] = mapped_column(String(32))

    # --- Applicability profile -------------------------------------------
    # These four are what the impact mapper (section 2.2, step 5) cross-
    # references a new regulation against, and what the obligations catalogue
    # keys its applicability rules off.
    state: Mapped[str | None] = mapped_column(String(64), index=True)
    industry: Mapped[str | None] = mapped_column(String(128), index=True)
    # Annual turnover in paise. Drives the GST composition/QRMP thresholds, the
    # tax-audit trigger and several MCA exemptions, so it is a first-class
    # profile field rather than something buried in ``metadata_json``.
    annual_turnover_paise: Mapped[int | None] = mapped_column(Paise)
    employee_count: Mapped[int | None] = mapped_column()
    incorporation_date: Mapped[date | None] = mapped_column(Date)
    financial_year_end_month: Mapped[int] = mapped_column(default=3, nullable=False)

    # Flags that switch whole families of obligations on. Kept as columns
    # rather than free-form JSON because the applicability engine reads them on
    # every catalogue evaluation and they need to be indexable.
    is_listed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_foreign_investment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    handles_personal_data: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- Contact ---------------------------------------------------------
    contact_email: Mapped[str | None] = mapped_column(String(255))
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    address: Mapped[str | None] = mapped_column(Text)

    # --- Commercials -----------------------------------------------------
    plan_tier: Mapped[PlanTier] = mapped_column(
        Enum(PlanTier, native_enum=False, length=32), default=PlanTier.SMB, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Anything that does not deserve a column yet. Not a dumping ground for
    # things the applicability engine reads — those get columns.
    metadata_json: Mapped[dict | None] = mapped_column(JSONType)

    users: Mapped[list["User"]] = relationship(  # noqa: F821,UP037
        back_populates="organization", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # A GSTIN or CIN identifies exactly one entity in India, so two live
        # organizations must not share one. Partial on ``deleted_at IS NULL``
        # so that soft-deleting an organization frees its identifiers for
        # re-onboarding — which happens, when a client is offboarded from one
        # CA firm and onboarded by another.
        #
        # The uniqueness is on the fingerprint, not the ciphertext: the
        # ciphertext differs on every write because the nonce does, so a
        # constraint on it would never fire.
        live_unique("uq_organizations_gstin", "gstin_fingerprint"),
        live_unique("uq_organizations_cin", "cin_fingerprint"),
        Index("ix_organizations_type_active", "type", "is_active"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Organization id={self.id} name={self.name!r} type={self.type}>"


class Client(Base, TimestampMixin, SoftDeleteMixin):
    """A CA firm's engagement with one client organization.

    The edge of the hierarchy, and the thing staff are assigned to. It is a
    table rather than a ``parent_org_id`` column on ``organizations`` because
    the edge carries data of its own — engagement type, dates, the staff member
    who owns it — and because a company can in principle be served by more than
    one firm across different periods.
    """

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Deliberately *not* OrgScopedMixin: this row spans two organizations, and
    # naming one of them ``organization_id`` would make it ambiguous which one
    # the tenant filter should use. The CA firm is the owner, so queries scope
    # on ``ca_firm_id``.
    ca_firm_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    client_org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    engagement_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)

    # Section 4.5: workload distribution among CA firm staff. Null means the
    # engagement is unassigned and shows up in the firm's unassigned queue.
    assigned_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    # Monthly retainer in paise, for the consolidated billing view.
    retainer_paise: Mapped[int | None] = mapped_column(Paise)
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSONType)

    ca_firm: Mapped[Organization] = relationship(foreign_keys=[ca_firm_id])
    client_org: Mapped[Organization] = relationship(foreign_keys=[client_org_id])

    __table_args__ = (
        # One live engagement per (firm, client) pair. Scoped on ``deleted_at``
        # for the same re-onboarding reason as above.
        live_unique("uq_clients_firm_client", "ca_firm_id", "client_org_id"),
        Index("ix_clients_firm_status", "ca_firm_id", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Client firm={self.ca_firm_id} client={self.client_org_id} {self.status}>"


# Imported for the relationship target; at the bottom to avoid a cycle, since
# User needs Organization.
from app.models.user import User  # noqa: E402,F401
