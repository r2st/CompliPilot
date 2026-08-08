"""FEMA obligations — inbound and outbound investment reporting.

Almost entirely event-based: FEMA does not ask for periodic returns so much as
for a report within N days of a transaction. The one exception, the annual FLA
return, lives in :mod:`app.data.catalogue.rbi` because it is filed on the RBI's
FLAIR portal and a user looking for it looks under RBI.

The single most expensive mistake in this family is a late FC-GPR. Thirty days
from allotment is a short window for a company that has just closed a funding
round, the Late Submission Fee scales with both the amount and the delay, and
the contravention has to be compounded before the next round's due diligence.
"""
from __future__ import annotations

from app.data.catalogue.base import COMPANIES_AND_LLPS, RUPEE, ObligationSpec
from app.models.enums import EntityType, Frequency, Regulation

_AUTHORITY = "Reserve Bank of India, under FEMA 1999"

_FEMA_PENALTY = (
    "Late Submission Fee per the RBI matrix; on compounding, up to three times the sum "
    "involved plus ₹5,000 per day of continuing contravention (section 13, FEMA 1999)"
)
_FEMA_PER_DAY = 5_000 * RUPEE

FEMA_OBLIGATIONS: tuple[ObligationSpec, ...] = (
    ObligationSpec(
        code="fema.fcgpr.event",
        title="FC-GPR — Reporting of shares issued to a non-resident",
        regulation=Regulation.FEMA,
        filing_type="FC-GPR",
        frequency=Frequency.EVENT_BASED,
        section="Regulation 4, FEM (Non-debt Instruments) Rules 2019 read with the "
        "Single Master Form directions",
        description=(
            "Filed on FIRMS within 30 days of allotting shares to a person resident "
            "outside India. The 30 days run from allotment, not from receipt of the "
            "money — a distinction that catches people out."
        ),
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=COMPANIES_AND_LLPS,
        requires_foreign_investment=True,
        penalty_description=_FEMA_PENALTY,
        penalty_per_day_paise=_FEMA_PER_DAY,
    ),
    ObligationSpec(
        code="fema.fctrs.event",
        title="FC-TRS — Reporting of a transfer of shares with a non-resident",
        regulation=Regulation.FEMA,
        filing_type="FC-TRS",
        frequency=Frequency.EVENT_BASED,
        section="Regulation 4, FEM (Non-debt Instruments) Rules 2019",
        description=(
            "Filed within 60 days of a transfer of shares between a resident and a "
            "non-resident, or of the receipt of consideration, whichever is earlier."
        ),
        authority=_AUTHORITY,
        offset_days=60,
        entity_types=COMPANIES_AND_LLPS,
        requires_foreign_investment=True,
        penalty_description=_FEMA_PENALTY,
        penalty_per_day_paise=_FEMA_PER_DAY,
    ),
    ObligationSpec(
        code="fema.arf.event",
        title="Advance Remittance Form — Receipt of foreign investment funds",
        regulation=Regulation.FEMA,
        filing_type="ARF",
        frequency=Frequency.EVENT_BASED,
        section="Master Direction — Reporting under FEMA 1999",
        description=(
            "The AD bank reports inward remittance towards a share subscription within "
            "30 days. Shares must then be allotted within 60 days of receipt or the "
            "money refunded."
        ),
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=COMPANIES_AND_LLPS,
        requires_foreign_investment=True,
        penalty_description=_FEMA_PENALTY,
        penalty_per_day_paise=_FEMA_PER_DAY,
    ),
    ObligationSpec(
        code="fema.allotment_window.event",
        title="Allotment of shares against an inward remittance",
        regulation=Regulation.FEMA,
        filing_type="Allotment",
        frequency=Frequency.EVENT_BASED,
        section="Rule 9, FEM (Non-debt Instruments) Rules 2019",
        description=(
            "Shares must be allotted within 60 days of receiving the subscription "
            "money, failing which the funds must be refunded within 15 days."
        ),
        authority=_AUTHORITY,
        offset_days=60,
        entity_types=COMPANIES_AND_LLPS,
        requires_foreign_investment=True,
        penalty_description=_FEMA_PENALTY,
        penalty_per_day_paise=_FEMA_PER_DAY,
    ),
    ObligationSpec(
        code="fema.lln.event",
        title="Form LLP-I — Foreign investment into an LLP",
        regulation=Regulation.FEMA,
        filing_type="LLP-I",
        frequency=Frequency.EVENT_BASED,
        section="Schedule VI, FEM (Non-debt Instruments) Rules 2019",
        description=(
            "Filed within 30 days of receiving a capital contribution into an LLP from "
            "a person resident outside India."
        ),
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=(EntityType.LLP,),
        requires_foreign_investment=True,
        penalty_description=_FEMA_PENALTY,
        penalty_per_day_paise=_FEMA_PER_DAY,
    ),
    ObligationSpec(
        code="fema.llp2.event",
        title="Form LLP-II — Disinvestment or transfer of an LLP interest",
        regulation=Regulation.FEMA,
        filing_type="LLP-II",
        frequency=Frequency.EVENT_BASED,
        section="Schedule VI, FEM (Non-debt Instruments) Rules 2019",
        description=(
            "Filed within 60 days of a transfer of capital contribution or profit share "
            "between a resident and a non-resident."
        ),
        authority=_AUTHORITY,
        offset_days=60,
        entity_types=(EntityType.LLP,),
        requires_foreign_investment=True,
        penalty_description=_FEMA_PENALTY,
        penalty_per_day_paise=_FEMA_PER_DAY,
    ),
    ObligationSpec(
        code="fema.dieai.event",
        title="Form DI — Downstream investment by a foreign-owned entity",
        regulation=Regulation.FEMA,
        filing_type="Form DI",
        frequency=Frequency.EVENT_BASED,
        section="Rule 23, FEM (Non-debt Instruments) Rules 2019",
        description=(
            "An Indian company owned or controlled by non-residents must report "
            "downstream investment into another Indian company within 30 days."
        ),
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=COMPANIES_AND_LLPS,
        requires_foreign_investment=True,
        penalty_description=_FEMA_PENALTY,
        penalty_per_day_paise=_FEMA_PER_DAY,
    ),
    ObligationSpec(
        code="fema.espop.event",
        title="Form ESOP — Grant of employee stock options to non-residents",
        regulation=Regulation.FEMA,
        filing_type="Form ESOP",
        frequency=Frequency.EVENT_BASED,
        section="Rule 8, FEM (Non-debt Instruments) Rules 2019",
        description=(
            "Filed within 30 days of issuing employee stock options to a person "
            "resident outside India."
        ),
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=COMPANIES_AND_LLPS,
        requires_foreign_investment=True,
        penalty_description=_FEMA_PENALTY,
        penalty_per_day_paise=_FEMA_PER_DAY,
    ),
    ObligationSpec(
        code="fema.cn.event",
        title="Form CN — Issue of convertible notes to a non-resident",
        regulation=Regulation.FEMA,
        filing_type="Form CN",
        frequency=Frequency.EVENT_BASED,
        section="Rule 4, FEM (Non-debt Instruments) Rules 2019",
        description=(
            "A startup issuing convertible notes to a person resident outside India "
            "reports within 30 days of issue."
        ),
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=COMPANIES_AND_LLPS,
        requires_foreign_investment=True,
        penalty_description=_FEMA_PENALTY,
        penalty_per_day_paise=_FEMA_PER_DAY,
    ),
    ObligationSpec(
        code="fema.odi_form.event",
        title="Form FC — Overseas direct investment",
        regulation=Regulation.FEMA,
        filing_type="Form FC",
        frequency=Frequency.EVENT_BASED,
        section="Regulation 10, FEM (Overseas Investment) Rules 2022",
        description=(
            "Filed through the AD bank within 30 days of making a financial commitment "
            "to an overseas entity, to obtain a Unique Identification Number."
        ),
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=COMPANIES_AND_LLPS,
        requires_foreign_investment=True,
        penalty_description=_FEMA_PENALTY,
        penalty_per_day_paise=_FEMA_PER_DAY,
    ),
)
