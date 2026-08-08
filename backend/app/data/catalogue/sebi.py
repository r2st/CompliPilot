"""SEBI obligations under the LODR and PIT Regulations.

Every row here carries ``requires_listed=True``. That is the whole applicability
rule for this family: SEBI's continuous-disclosure regime binds listed entities
and nobody else, and an unlisted private limited company with a ₹200 crore
turnover owes none of it. Making the flag explicit on each row rather than
implicit in the module keeps the applicability engine's reasoning legible when
it explains itself to a user.

Deadlines here are almost all "within N days of the quarter end", which the
engine expresses directly with ``offset_days`` — no month arithmetic and no
approximation.
"""
from __future__ import annotations

from app.data.catalogue.base import COMPANIES, LAKH, RUPEE, ObligationSpec
from app.models.enums import EntityType, Frequency, Regulation

_AUTHORITY = "Securities and Exchange Board of India (SEBI)"

# Schedule III of the LODR sets a per-day fine per non-compliance, levied by the
# exchange. ₹5,000 a day is the common band for a late periodic filing.
_LODR_PER_DAY = 5_000 * RUPEE

_LISTED = (EntityType.PUBLIC_LIMITED,)

SEBI_OBLIGATIONS: tuple[ObligationSpec, ...] = (
    ObligationSpec(
        code="sebi.shareholding.quarterly",
        title="Shareholding pattern",
        regulation=Regulation.SEBI,
        filing_type="Reg 31 SHP",
        frequency=Frequency.QUARTERLY,
        section="Regulation 31, SEBI (LODR) Regulations 2015",
        description=(
            "Quarterly statement of shareholding, filed with the stock exchange within "
            "21 days of the quarter end."
        ),
        authority=_AUTHORITY,
        offset_days=21,
        entity_types=_LISTED,
        requires_listed=True,
        penalty_description="₹2,000 per day of delay levied by the exchange (Schedule III)",
        penalty_per_day_paise=2_000 * RUPEE,
    ),
    ObligationSpec(
        code="sebi.corp_governance.quarterly",
        title="Corporate governance report",
        regulation=Regulation.SEBI,
        filing_type="Reg 27(2) CG",
        frequency=Frequency.QUARTERLY,
        section="Regulation 27(2), SEBI (LODR) Regulations 2015",
        description=(
            "Quarterly compliance report on corporate governance — board composition, "
            "committee constitution and meeting attendance — within 21 days of the "
            "quarter end."
        ),
        authority=_AUTHORITY,
        offset_days=21,
        entity_types=_LISTED,
        requires_listed=True,
        penalty_description="₹2,000 per day of delay levied by the exchange",
        penalty_per_day_paise=2_000 * RUPEE,
    ),
    ObligationSpec(
        code="sebi.financial_results.quarterly",
        title="Quarterly financial results",
        regulation=Regulation.SEBI,
        filing_type="Reg 33 Results",
        frequency=Frequency.QUARTERLY,
        section="Regulation 33, SEBI (LODR) Regulations 2015",
        description=(
            "Standalone and consolidated results with a limited review report, within "
            "45 days of the quarter end. The fourth quarter's are annual audited "
            "results and get 60 days instead."
        ),
        authority=_AUTHORITY,
        offset_days=45,
        entity_types=_LISTED,
        requires_listed=True,
        penalty_description="₹5,000 per day of delay levied by the exchange",
        penalty_per_day_paise=_LODR_PER_DAY,
        metadata={
            "approximation": (
                "The January–March quarter's audited annual results get 60 days, not "
                "45. This rule applies 45 to every quarter, so Q4 warns 15 days early."
            )
        },
    ),
    ObligationSpec(
        code="sebi.annual_results.annual",
        title="Annual audited financial results",
        regulation=Regulation.SEBI,
        filing_type="Reg 33(3)(d)",
        frequency=Frequency.ANNUAL,
        section="Regulation 33(3)(d), SEBI (LODR) Regulations 2015",
        description="Audited annual results within 60 days of the financial year end.",
        authority=_AUTHORITY,
        offset_days=60,
        period_offset=0,
        entity_types=_LISTED,
        requires_listed=True,
        penalty_description="₹5,000 per day of delay levied by the exchange",
        penalty_per_day_paise=_LODR_PER_DAY,
    ),
    ObligationSpec(
        code="sebi.annual_report.annual",
        title="Annual report to shareholders and the exchange",
        regulation=Regulation.SEBI,
        filing_type="Reg 34",
        frequency=Frequency.ANNUAL,
        section="Regulation 34, SEBI (LODR) Regulations 2015",
        description=(
            "The annual report, sent to shareholders and filed with the exchange not "
            "later than the day it is dispatched."
        ),
        authority=_AUTHORITY,
        due_month=9,
        due_day=30,
        entity_types=_LISTED,
        requires_listed=True,
        penalty_description="₹2,000 per day of delay levied by the exchange",
        penalty_per_day_paise=2_000 * RUPEE,
    ),
    ObligationSpec(
        code="sebi.secretarial_audit.annual",
        title="MR-3 — Secretarial audit report and annual secretarial compliance report",
        regulation=Regulation.SEBI,
        filing_type="MR-3",
        frequency=Frequency.ANNUAL,
        section="Regulation 24A, SEBI (LODR) Regulations 2015",
        description=(
            "Annual secretarial compliance report from a practising company secretary, "
            "filed within 60 days of the year end."
        ),
        authority=_AUTHORITY,
        offset_days=60,
        period_offset=0,
        entity_types=COMPANIES,
        requires_listed=True,
        penalty_description="₹2,000 per day of delay levied by the exchange",
        penalty_per_day_paise=2_000 * RUPEE,
    ),
    ObligationSpec(
        code="sebi.investor_grievance.quarterly",
        title="Statement of investor complaints",
        regulation=Regulation.SEBI,
        filing_type="Reg 13(3)",
        frequency=Frequency.QUARTERLY,
        section="Regulation 13(3), SEBI (LODR) Regulations 2015",
        description=(
            "Quarterly statement of investor complaints received, disposed of and "
            "pending, within 21 days of the quarter end."
        ),
        authority=_AUTHORITY,
        offset_days=21,
        entity_types=_LISTED,
        requires_listed=True,
        penalty_description="₹1,000 per day of delay levied by the exchange",
        penalty_per_day_paise=1_000 * RUPEE,
    ),
    ObligationSpec(
        code="sebi.reconciliation_share_capital.quarterly",
        title="Reconciliation of share capital audit report",
        regulation=Regulation.SEBI,
        filing_type="RSCA",
        frequency=Frequency.QUARTERLY,
        section="Regulation 76, SEBI (Depositories and Participants) Regulations 2018",
        description=(
            "Quarterly reconciliation between issued capital and the capital held in "
            "dematerialised and physical form, within 30 days of the quarter end."
        ),
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=_LISTED,
        requires_listed=True,
        penalty_description="₹2,000 per day of delay levied by the exchange",
        penalty_per_day_paise=2_000 * RUPEE,
    ),
    ObligationSpec(
        code="sebi.board_meeting_intimation.event",
        title="Prior intimation of a board meeting",
        regulation=Regulation.SEBI,
        filing_type="Reg 29",
        frequency=Frequency.EVENT_BASED,
        section="Regulation 29, SEBI (LODR) Regulations 2015",
        description=(
            "At least five clear days' notice to the exchange before a board meeting "
            "at which financial results are considered, and two days for most others. "
            "Recorded with a zero offset because the deadline is the meeting date "
            "itself minus the notice period, which the filing's own due date carries."
        ),
        authority=_AUTHORITY,
        offset_days=0,
        entity_types=_LISTED,
        requires_listed=True,
        penalty_description="₹10,000 per instance levied by the exchange",
        penalty_max_paise=10_000 * RUPEE,
    ),
    ObligationSpec(
        code="sebi.material_event.event",
        title="Disclosure of a material event",
        regulation=Regulation.SEBI,
        filing_type="Reg 30",
        frequency=Frequency.EVENT_BASED,
        section="Regulation 30 read with Schedule III, SEBI (LODR) Regulations 2015",
        description=(
            "Material events must be disclosed to the exchange within 12 hours where "
            "the event originates inside the company, and 24 hours otherwise. Recorded "
            "as one day, which is the shorter of the two rounded to the engine's "
            "date resolution."
        ),
        authority=_AUTHORITY,
        offset_days=1,
        entity_types=_LISTED,
        requires_listed=True,
        penalty_description="₹10,000 per instance, rising for continuing non-disclosure",
        penalty_max_paise=10_000 * RUPEE,
    ),
    ObligationSpec(
        code="sebi.pit_disclosure.event",
        title="Insider trading disclosure — Form C",
        regulation=Regulation.SEBI,
        filing_type="PIT Form C",
        frequency=Frequency.EVENT_BASED,
        section="Regulation 7(2), SEBI (Prohibition of Insider Trading) Regulations 2015",
        description=(
            "A promoter, director or designated person must disclose a trade above "
            "₹10 lakh in value within two trading days, and the company must pass it "
            "to the exchange within two more."
        ),
        authority=_AUTHORITY,
        offset_days=2,
        entity_types=_LISTED,
        requires_listed=True,
        penalty_description="Up to ₹10 lakh or three times the profit, under section 15G",
        penalty_max_paise=10 * LAKH,
    ),
    ObligationSpec(
        code="sebi.trading_window.quarterly",
        title="Trading window closure notice",
        regulation=Regulation.SEBI,
        filing_type="PIT Reg 9",
        frequency=Frequency.QUARTERLY,
        section="Clause 4, Schedule B, SEBI (PIT) Regulations 2015",
        description=(
            "The trading window must be closed for designated persons from the end of "
            "each quarter until 48 hours after the results are published. The notice "
            "goes out at the quarter end."
        ),
        authority=_AUTHORITY,
        offset_days=0,
        entity_types=_LISTED,
        requires_listed=True,
        penalty_description="Enforcement action under the PIT Regulations",
    ),
    ObligationSpec(
        code="sebi.large_corporate.annual",
        title="Large corporate borrower disclosure",
        regulation=Regulation.SEBI,
        filing_type="LC Disclosure",
        frequency=Frequency.ANNUAL,
        section="SEBI Circular on Fund Raising by Large Corporates",
        description=(
            "An entity classified as a large corporate must disclose its incremental "
            "borrowing and the share raised through debt securities, within 45 days of "
            "the year end."
        ),
        authority=_AUTHORITY,
        offset_days=45,
        period_offset=0,
        entity_types=_LISTED,
        requires_listed=True,
        penalty_description="Monetary penalty per the SEBI circular on shortfall in debt raising",
    ),
)
