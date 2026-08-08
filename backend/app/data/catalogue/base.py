"""The shape of one catalogue entry, and the units its money fields use.

Every obligation in :mod:`app.data.catalogue` is an :class:`ObligationSpec`.
Using a frozen dataclass rather than a bare dict buys three things that matter
for a table the whole product is downstream of: a typo in a field name is an
error at import rather than a silently ignored key, the due-date rule can be
sanity-checked in one place, and the catalogue can be enumerated by the test
suite without a database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.models.enums import EntityType, Frequency, Regulation

# Money in the catalogue is written in rupees at the call site — "₹200 per day"
# — and converted here, because a penalty written as 20000 paise is a penalty
# nobody will notice is wrong.
RUPEE = 100
LAKH = 100_000 * RUPEE
CRORE = 100 * LAKH


# Entity groupings used over and over by the applicability rules. Named so a
# catalogue entry reads "companies_and_llps" rather than repeating a six-member
# list that is easy to get subtly different between two obligations.
COMPANIES: tuple[EntityType, ...] = (
    EntityType.PRIVATE_LIMITED,
    EntityType.PUBLIC_LIMITED,
    EntityType.ONE_PERSON_COMPANY,
    EntityType.SECTION_8,
)
COMPANIES_AND_LLPS: tuple[EntityType, ...] = (*COMPANIES, EntityType.LLP)
# Everything that can hold a GSTIN and file returns. Trusts and societies are
# included: a society running a canteen registers like anyone else.
GST_REGISTRABLE: tuple[EntityType, ...] = (
    *COMPANIES_AND_LLPS,
    EntityType.PARTNERSHIP,
    EntityType.PROPRIETORSHIP,
    EntityType.TRUST,
    EntityType.SOCIETY,
    EntityType.HUF,
    EntityType.FOREIGN_COMPANY,
)
# Anyone who can employ staff, which is the trigger for the labour-law family.
EMPLOYERS: tuple[EntityType, ...] = GST_REGISTRABLE


@dataclass(frozen=True)
class ObligationSpec:
    """One row of the system obligations catalogue.

    The field names match :class:`~app.models.obligation.ComplianceObligation`
    one-for-one, so :func:`app.data.seed.seed_obligations` can copy across
    without a translation table that would drift.

    **On the due-date fields.** Exactly one of the three rules described in
    :func:`app.services.deadline_engine.due_date_for` should be expressed:
    ``offset_days`` for "within N days of the period ending", ``due_month``
    (plus ``due_day``) for a fixed calendar date, or ``due_day`` alone for "the
    Nth of the following period". :meth:`validate` refuses an entry that sets
    more than one, because the engine would silently take the first and the
    catalogue author would never learn which.
    """

    code: str
    title: str
    regulation: Regulation
    frequency: Frequency

    filing_type: str | None = None
    section: str | None = None
    description: str | None = None
    authority: str | None = None

    # --- Due-date rule ----------------------------------------------------
    due_day: int | None = None
    due_month: int | None = None
    offset_days: int | None = None
    period_offset: int = 1

    # --- Applicability ----------------------------------------------------
    entity_types: tuple[EntityType, ...] | None = None
    states: tuple[str, ...] | None = None
    industries: tuple[str, ...] | None = None
    min_turnover_paise: int | None = None
    max_turnover_paise: int | None = None
    min_employees: int | None = None
    requires_listed: bool | None = None
    requires_foreign_investment: bool | None = None
    requires_personal_data: bool | None = None

    # --- Consequences -----------------------------------------------------
    penalty_description: str | None = None
    penalty_per_day_paise: int | None = None
    penalty_max_paise: int | None = None

    effective_from: date | None = None
    effective_to: date | None = None

    # Free-form notes that travel with the row. Used here to record where a
    # rule is an approximation — see the TDS quarterly entries, whose Q4 due
    # date the engine cannot express and which therefore warn a month early.
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Refuse an entry the deadline engine would misread.

        Raises rather than warns. The catalogue is imported at startup and by
        the test suite, so a bad entry fails loudly on a developer's machine
        instead of producing a wrong due date on a client's calendar.
        """
        rules = sum(
            1 for value in (self.offset_days, self.due_month, self.due_day) if value is not None
        )
        # due_month and due_day together are one rule, not two.
        if self.due_month is not None and self.due_day is not None:
            rules -= 1

        periodic = self.frequency not in (Frequency.EVENT_BASED, Frequency.ONE_TIME)

        if rules > 1:
            raise ValueError(
                f"{self.code}: sets more than one due-date rule; "
                "pick offset_days, due_month(+due_day), or due_day"
            )
        if periodic and rules == 0:
            raise ValueError(f"{self.code}: a recurring obligation needs a due-date rule")
        if not periodic and self.offset_days is None:
            raise ValueError(
                f"{self.code}: an event-based or one-time obligation needs offset_days, "
                "measured from the event"
            )
        if self.due_month is not None and not 1 <= self.due_month <= 12:
            raise ValueError(f"{self.code}: due_month {self.due_month} is not a month")
        if self.due_day is not None and not 1 <= self.due_day <= 31:
            raise ValueError(f"{self.code}: due_day {self.due_day} is not a day")
        if (
            self.min_turnover_paise is not None
            and self.max_turnover_paise is not None
            and self.min_turnover_paise > self.max_turnover_paise
        ):
            raise ValueError(f"{self.code}: turnover band is inverted")

    def to_columns(self) -> dict:
        """The row as :class:`ComplianceObligation` keyword arguments."""
        return {
            "code": self.code,
            "title": self.title,
            "regulation": self.regulation,
            "frequency": self.frequency,
            "filing_type": self.filing_type,
            "section": self.section,
            "description": self.description,
            "authority": self.authority,
            "due_day": self.due_day,
            "due_month": self.due_month,
            "offset_days": self.offset_days,
            "period_offset": self.period_offset,
            "entity_types_json": (
                [str(e) for e in self.entity_types] if self.entity_types else None
            ),
            "states_json": list(self.states) if self.states else None,
            "industries_json": list(self.industries) if self.industries else None,
            "min_turnover_paise": self.min_turnover_paise,
            "max_turnover_paise": self.max_turnover_paise,
            "min_employees": self.min_employees,
            "requires_listed": self.requires_listed,
            "requires_foreign_investment": self.requires_foreign_investment,
            "requires_personal_data": self.requires_personal_data,
            "penalty_description": self.penalty_description,
            "penalty_per_day_paise": self.penalty_per_day_paise,
            "penalty_max_paise": self.penalty_max_paise,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "metadata_json": dict(self.metadata) or None,
            "is_system": True,
            "is_active": True,
            "organization_id": None,
        }
