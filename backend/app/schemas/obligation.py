"""Obligations catalogue and per-organization applicability bodies."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

from app.models.enums import Frequency, Regulation
from app.schemas.common import ORMModel, Paise


class ObligationResponse(ORMModel):
    """One catalogue entry, as the browse endpoint returns it."""

    id: int
    organization_id: int | None = None
    regulation: Regulation
    code: str
    title: str
    filing_type: str | None = None
    section: str | None = None
    description: str | None = None
    authority: str | None = None
    frequency: Frequency

    due_day: int | None = None
    due_month: int | None = None
    offset_days: int | None = None
    period_offset: int = 1

    entity_types_json: list | None = None
    states_json: list | None = None
    industries_json: list | None = None
    min_turnover_paise: int | None = None
    max_turnover_paise: int | None = None
    min_employees: int | None = None
    requires_listed: bool | None = None
    requires_foreign_investment: bool | None = None
    requires_personal_data: bool | None = None

    penalty_description: str | None = None
    penalty_per_day_paise: int | None = None
    penalty_max_paise: int | None = None

    effective_from: date | None = None
    effective_to: date | None = None
    is_active: bool
    is_system: bool
    created_at: datetime


class ObligationCreateRequest(BaseModel):
    """A CA firm's own obligation, alongside the system catalogue.

    The due-date rule is validated here with the same three-way exclusivity
    :class:`app.data.catalogue.base.ObligationSpec` enforces on the system
    catalogue. Duplicating the rule is worth it: without the check, a
    tenant-authored obligation that set both ``offset_days`` and ``due_day``
    would take whichever the engine happened to try first, and the author would
    have no way to discover which.
    """

    regulation: Regulation
    code: str = Field(min_length=2, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    title: str = Field(min_length=2, max_length=255)
    frequency: Frequency

    filing_type: str | None = Field(default=None, max_length=64)
    section: str | None = Field(default=None, max_length=255)
    description: str | None = None
    authority: str | None = Field(default=None, max_length=128)

    due_day: int | None = Field(default=None, ge=1, le=31)
    due_month: int | None = Field(default=None, ge=1, le=12)
    offset_days: int | None = Field(default=None, ge=0, le=3650)
    period_offset: int = Field(default=1, ge=0, le=12)

    entity_types_json: list[str] | None = None
    states_json: list[str] | None = None
    industries_json: list[str] | None = None
    min_turnover_paise: Paise | None = None
    max_turnover_paise: Paise | None = None
    min_employees: int | None = Field(default=None, ge=0)
    requires_listed: bool | None = None
    requires_foreign_investment: bool | None = None
    requires_personal_data: bool | None = None

    penalty_description: str | None = None
    penalty_per_day_paise: Paise | None = None
    penalty_max_paise: Paise | None = None
    effective_from: date | None = None
    effective_to: date | None = None

    @model_validator(mode="after")
    def _one_due_date_rule(self):
        rules = sum(
            1 for v in (self.offset_days, self.due_month, self.due_day) if v is not None
        )
        if self.due_month is not None and self.due_day is not None:
            rules -= 1

        periodic = self.frequency not in (Frequency.EVENT_BASED, Frequency.ONE_TIME)
        if rules > 1:
            raise ValueError(
                "Set exactly one due-date rule: offset_days, due_month (with an "
                "optional due_day), or due_day alone"
            )
        if periodic and rules == 0:
            raise ValueError("A recurring obligation needs a due-date rule")
        if not periodic and self.offset_days is None:
            raise ValueError(
                "An event-based or one-time obligation needs offset_days, "
                "measured from the event"
            )
        if (
            self.min_turnover_paise is not None
            and self.max_turnover_paise is not None
            and self.min_turnover_paise > self.max_turnover_paise
        ):
            raise ValueError("The turnover band is inverted")
        return self


class ObligationUpdateRequest(BaseModel):
    """Edit a tenant's own obligation. System rows are never editable."""

    title: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = None
    section: str | None = Field(default=None, max_length=255)
    penalty_description: str | None = None
    penalty_per_day_paise: Paise | None = None
    penalty_max_paise: Paise | None = None
    effective_to: date | None = None
    is_active: bool | None = None


class OrganizationObligationResponse(ORMModel):
    """What one organization owes, and why we think so."""

    id: int
    organization_id: int
    obligation_id: int
    is_applicable_override: bool | None = None
    engine_verdict: bool | None = None
    engine_reason: str | None = None
    frequency_override: Frequency | None = None
    due_day_override: int | None = None
    owner_user_id: int | None = None
    notes: str | None = None
    created_at: datetime

    # The effective answer, which is the override if set and the engine's
    # otherwise. Computed rather than stored — see
    # :attr:`app.models.obligation.OrganizationObligation.is_applicable`.
    is_applicable: bool = False
    obligation: ObligationResponse | None = None


class OrganizationObligationUpdateRequest(BaseModel):
    """A human's decision about one obligation.

    ``is_applicable_override`` is tri-state on purpose: ``true`` and ``false``
    are decisions, and ``null`` hands the question back to the engine. Without
    the third value there would be no way to undo an override, and a CA who
    marked something exempt by mistake would be stuck with it.
    """

    is_applicable_override: bool | None = None
    frequency_override: Frequency | None = None
    due_day_override: int | None = Field(default=None, ge=1, le=31)
    owner_user_id: int | None = None
    notes: str | None = Field(default=None, max_length=4000)

    # Distinguishes "the key was absent" from "the key was sent as null",
    # which Pydantic cannot otherwise express and which is the difference
    # between leaving an override alone and clearing it.
    clear_override: bool = False


class ApplicabilitySyncResponse(BaseModel):
    """The result of re-running the engine over one organization."""

    evaluated: int
    created: int
    verdict_changed: int
    uncertain: int
    conflicts: list[int]


class CoverageResponse(BaseModel):
    """Per-regulation applicable / not-applicable / overridden counts."""

    by_regulation: dict[str, dict[str, int]]
    total_applicable: int
    total_evaluated: int
