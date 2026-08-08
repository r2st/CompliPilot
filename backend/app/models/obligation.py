"""The compliance obligations catalogue and each organization's subscription to it."""
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
from app.models.enums import Frequency, Regulation
from app.models.mixins import (
    JSONType,
    Paise,
    SoftDeleteMixin,
    TimestampMixin,
    live_unique,
)


class ComplianceObligation(Base, TimestampMixin, SoftDeleteMixin):
    """One regulatory requirement — "file GSTR-3B monthly", "file MGT-7 annually".

    This is the *catalogue*, not a thing anyone owes yet. Rows are mostly
    system-owned: the CompliPilot team curates the ~200 obligations of section
    5 and updates them when a regulation changes. A CA firm may also add
    private ones, which is why ``organization_id`` exists and is nullable —
    null means "system catalogue, visible to every tenant".

    That nullable tenant column is the one deliberate exception to the
    ``OrgScopedMixin`` rule, and it is why this model does not carry the mixin.
    Reads go through :func:`app.core.tenancy.catalogue_scoped`, which filters
    ``organization_id IS NULL OR organization_id = :org``; writes to a
    system row are refused for every tenant.
    """

    __tablename__ = "compliance_obligations"

    id: Mapped[int] = mapped_column(primary_key=True)

    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )

    regulation: Mapped[Regulation] = mapped_column(
        Enum(Regulation, native_enum=False, length=32), nullable=False, index=True
    )
    # Stable machine key, e.g. "gst.gstr3b.monthly" or "mca.mgt7.annual". This
    # is what seed data, templates and the AI generator join on, so it survives
    # a change of ``title`` and is unique across the system catalogue.
    code: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # The filing's official designation — "GSTR-3B", "MGT-7", "FC-GPR".
    filing_type: Mapped[str | None] = mapped_column(String(64), index=True)
    # Statutory anchor: "Section 39, CGST Act 2017". Printed on the alert so a
    # CA can check the source rather than take our word for it.
    section: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    authority: Mapped[str | None] = mapped_column(String(128))

    frequency: Mapped[Frequency] = mapped_column(
        Enum(Frequency, native_enum=False, length=32), nullable=False
    )

    # --- Due-date rule ---------------------------------------------------
    # Interpreted by :mod:`app.services.deadline_engine`. Split into columns
    # rather than left in JSON because every one of them is read on every
    # deadline computation, and because a typo in a JSON key is a silently
    # wrong due date rather than an error.
    #
    # ``due_day`` is the day of the target month (GSTR-3B: 20). ``due_month``
    # is the month for an annual obligation (MGT-7: 10, meaning October).
    # ``offset_days`` is used by the event-based and quarter-relative rules
    # (SEBI shareholding: 21 days after quarter end).
    due_day: Mapped[int | None] = mapped_column()
    due_month: Mapped[int | None] = mapped_column()
    offset_days: Mapped[int | None] = mapped_column()
    # How many periods after the period being reported the due date falls in.
    # GSTR-3B for April is due in May, so 1. An annual return for FY2024-25 is
    # due in the following financial year, so also 1.
    period_offset: Mapped[int] = mapped_column(default=1, nullable=False)

    # --- Applicability ---------------------------------------------------
    # Evaluated against the organization profile by
    # :mod:`app.services.applicability`. Null in any of these means "no
    # constraint on this axis" — which is the common case, and is why they are
    # nullable rather than defaulting to an empty list that would match
    # nothing.
    entity_types_json: Mapped[list | None] = mapped_column(JSONType)
    states_json: Mapped[list | None] = mapped_column(JSONType)
    industries_json: Mapped[list | None] = mapped_column(JSONType)
    min_turnover_paise: Mapped[int | None] = mapped_column(Paise)
    max_turnover_paise: Mapped[int | None] = mapped_column(Paise)
    min_employees: Mapped[int | None] = mapped_column()
    requires_listed: Mapped[bool | None] = mapped_column(Boolean)
    requires_foreign_investment: Mapped[bool | None] = mapped_column(Boolean)
    requires_personal_data: Mapped[bool | None] = mapped_column(Boolean)

    # --- Consequences ----------------------------------------------------
    # Surfaced on the deadline card, because "₹200/day" is what actually moves
    # an SMB to file on time.
    penalty_description: Mapped[str | None] = mapped_column(Text)
    penalty_per_day_paise: Mapped[int | None] = mapped_column(Paise)
    penalty_max_paise: Mapped[int | None] = mapped_column(Paise)

    # When this obligation came into force and, if amended away, when it
    # stopped. The deadline engine will not generate a filing for a period
    # outside this window — which is what keeps a repealed return from
    # reappearing on next year's calendar.
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Set on rows the CompliPilot team maintains. A tenant may not edit these.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    metadata_json: Mapped[dict | None] = mapped_column(JSONType)

    __table_args__ = (
        # Unique per owner: the system catalogue may hold one "gst.gstr3b" and
        # a firm may hold its own override under the same code.
        live_unique("uq_obligations_org_code", "organization_id", "code"),
        Index("ix_obligations_regulation_active", "regulation", "is_active"),
    )

    @property
    def is_recurring(self) -> bool:
        return self.frequency not in (Frequency.EVENT_BASED, Frequency.ONE_TIME)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ComplianceObligation {self.code} {self.frequency}>"


class OrganizationObligation(Base, TimestampMixin, SoftDeleteMixin):
    """A catalogue obligation as it applies to one organization.

    The applicability engine proposes these; a human confirms or overrides
    them. The override is the point of the table: applicability rules are
    heuristics over an incomplete profile, and a CA who knows their client is
    exempt must be able to say so without the next catalogue sync undoing it.
    """

    __tablename__ = "organization_obligations"

    id: Mapped[int] = mapped_column(primary_key=True)

    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    obligation_id: Mapped[int] = mapped_column(
        ForeignKey("compliance_obligations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Null means "follow the engine". True/False is a human decision that the
    # engine must not overwrite — which is enforced in
    # :func:`app.services.applicability.sync_organization_obligations`.
    is_applicable_override: Mapped[bool | None] = mapped_column(Boolean)
    # What the engine last concluded, kept alongside so the UI can show "we
    # think this applies; you have marked it exempt".
    engine_verdict: Mapped[bool | None] = mapped_column(Boolean)
    engine_reason: Mapped[str | None] = mapped_column(Text)

    # Per-organization schedule tweaks. A QRMP taxpayer files GSTR-3B
    # quarterly rather than monthly, which is the same obligation on a
    # different cadence.
    frequency_override: Mapped[Frequency | None] = mapped_column(
        Enum(Frequency, native_enum=False, length=32)
    )
    due_day_override: Mapped[int | None] = mapped_column()

    # Whom to chase. Falls back to the organization's compliance managers.
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    notes: Mapped[str | None] = mapped_column(Text)

    obligation: Mapped[ComplianceObligation] = relationship()

    __table_args__ = (
        live_unique("uq_org_obligation", "organization_id", "obligation_id"),
        Index("ix_org_obligations_org_created", "organization_id", "created_at"),
    )

    @property
    def is_applicable(self) -> bool:
        """The effective verdict: the human override if there is one, else the engine's.

        Defaults to ``False`` when neither has an opinion. Generating filings a
        client does not owe is worse than generating none: it trains people to
        ignore the calendar.
        """
        if self.is_applicable_override is not None:
            return self.is_applicable_override
        return bool(self.engine_verdict)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OrganizationObligation org={self.organization_id} ob={self.obligation_id}>"
