"""RBI obligations — foreign liabilities and assets, external borrowings, NBFCs.

Narrower than GST or MCA, and mostly conditional: an SMB with no foreign
investment and no overseas borrowing owes almost nothing here. That is why
nearly every row below carries ``requires_foreign_investment`` or an industry
constraint — an unconditional RBI row would put a return on every client's
calendar that ninety of a hundred of them do not owe, and a calendar that cries
wolf is one people stop reading.
"""
from __future__ import annotations

from app.data.catalogue.base import (
    COMPANIES_AND_LLPS,
    CRORE,
    LAKH,
    RUPEE,
    ObligationSpec,
)
from app.models.enums import Frequency, Regulation

_AUTHORITY = "Reserve Bank of India (RBI)"

# The general FEMA/RBI contravention penalty, applied through compounding. Cited
# as a range because that is what the statute gives.
_FEMA_PENALTY = (
    "Up to three times the sum involved where quantifiable, or ₹2,00,000 otherwise, "
    "plus ₹5,000 per day of continuing contravention (section 13, FEMA 1999)"
)

RBI_OBLIGATIONS: tuple[ObligationSpec, ...] = (
    ObligationSpec(
        code="rbi.fla.annual",
        title="FLA — Annual return on foreign liabilities and assets",
        regulation=Regulation.RBI,
        filing_type="FLA",
        frequency=Frequency.ANNUAL,
        section="FEMA 1999 read with the FLA Return directions",
        description=(
            "Annual return by any Indian entity that has received foreign direct "
            "investment or made overseas direct investment, filed on the FLAIR portal "
            "by 15 July on the basis of unaudited accounts if the audit is not done."
        ),
        authority=_AUTHORITY,
        due_month=7,
        due_day=15,
        entity_types=COMPANIES_AND_LLPS,
        requires_foreign_investment=True,
        penalty_description=_FEMA_PENALTY,
        penalty_per_day_paise=5_000 * RUPEE,
    ),
    ObligationSpec(
        code="rbi.ecb2.monthly",
        title="ECB-2 — Monthly return on external commercial borrowings",
        regulation=Regulation.RBI,
        filing_type="ECB-2",
        frequency=Frequency.MONTHLY,
        section="Master Direction — External Commercial Borrowings, Trade Credits and "
        "Structured Obligations",
        description=(
            "Monthly return on drawdowns and repayments under an ECB, filed through "
            "the AD Category-I bank by the 7th of the following month. Continues for "
            "the life of the loan, including nil months."
        ),
        authority=_AUTHORITY,
        due_day=7,
        entity_types=COMPANIES_AND_LLPS,
        requires_foreign_investment=True,
        penalty_description="Late Submission Fee per the RBI's LSF matrix, plus " + _FEMA_PENALTY,
        penalty_per_day_paise=5_000 * RUPEE,
    ),
    ObligationSpec(
        code="rbi.ecb_form.event",
        title="Form ECB — Reporting of a new borrowing",
        regulation=Regulation.RBI,
        filing_type="Form ECB",
        frequency=Frequency.EVENT_BASED,
        section="Master Direction — External Commercial Borrowings",
        description=(
            "Filed to obtain a Loan Registration Number before any drawdown under a "
            "new external commercial borrowing."
        ),
        authority=_AUTHORITY,
        offset_days=7,
        entity_types=COMPANIES_AND_LLPS,
        requires_foreign_investment=True,
        penalty_description=_FEMA_PENALTY,
    ),
    ObligationSpec(
        code="rbi.odi_apr.annual",
        title="APR — Annual performance report on overseas investment",
        regulation=Regulation.RBI,
        filing_type="Form APR",
        frequency=Frequency.ANNUAL,
        section="Regulation 10, Foreign Exchange Management (Overseas Investment) Rules 2022",
        description=(
            "Annual report on each overseas joint venture or wholly owned subsidiary, "
            "based on its audited accounts, due 31 December."
        ),
        authority=_AUTHORITY,
        due_month=12,
        due_day=31,
        entity_types=COMPANIES_AND_LLPS,
        requires_foreign_investment=True,
        penalty_description=_FEMA_PENALTY,
        penalty_per_day_paise=5_000 * RUPEE,
    ),
    ObligationSpec(
        code="rbi.nbfc_dnbs03.quarterly",
        title="DNBS-03 — NBFC prudential returns",
        regulation=Regulation.RBI,
        filing_type="DNBS-03",
        frequency=Frequency.QUARTERLY,
        section="Master Direction — Non-Banking Financial Company Returns",
        description=(
            "Quarterly prudential return on capital adequacy, asset classification and "
            "provisioning, filed on the RBI's XBRL portal within 21 days of quarter end."
        ),
        authority=_AUTHORITY,
        offset_days=21,
        entity_types=COMPANIES_AND_LLPS,
        industries=("nbfc", "financial-services", "lending"),
        penalty_description="Up to ₹5,000 per day of delay under section 58B, RBI Act 1934",
        penalty_per_day_paise=5_000 * RUPEE,
    ),
    ObligationSpec(
        code="rbi.nbfc_dnbs10.annual",
        title="DNBS-10 — NBFC statutory auditor's certificate",
        regulation=Regulation.RBI,
        filing_type="DNBS-10",
        frequency=Frequency.ANNUAL,
        section="Master Direction — Non-Banking Financial Company Returns",
        description=(
            "Annual certificate from the statutory auditor confirming continued "
            "eligibility to hold the certificate of registration, due 30 June."
        ),
        authority=_AUTHORITY,
        due_month=6,
        due_day=30,
        entity_types=COMPANIES_AND_LLPS,
        industries=("nbfc", "financial-services", "lending"),
        penalty_description="Up to ₹5,000 per day of delay under section 58B, RBI Act 1934",
        penalty_per_day_paise=5_000 * RUPEE,
    ),
    ObligationSpec(
        code="rbi.softex.monthly",
        title="SOFTEX — Declaration of software exports",
        regulation=Regulation.RBI,
        filing_type="SOFTEX",
        frequency=Frequency.MONTHLY,
        section="Regulation 6, Foreign Exchange Management (Export of Goods and Services) "
        "Regulations 2015",
        description=(
            "Monthly declaration of software and IT-enabled service exports, certified "
            "by STPI and filed within 30 days of the invoice."
        ),
        authority=_AUTHORITY,
        due_day=30,
        entity_types=COMPANIES_AND_LLPS,
        industries=("software-export", "it-services", "software", "bpo"),
        penalty_description=_FEMA_PENALTY,
    ),
    ObligationSpec(
        code="rbi.edpms.event",
        title="EDPMS — Realisation of export proceeds",
        regulation=Regulation.RBI,
        filing_type="EDPMS",
        frequency=Frequency.EVENT_BASED,
        section="Master Direction — Export of Goods and Services",
        description=(
            "Export proceeds must be realised and repatriated within nine months of "
            "shipment, and the shipping bill closed on EDPMS. An open bill past the "
            "window is a FEMA contravention the AD bank will chase."
        ),
        authority=_AUTHORITY,
        offset_days=270,
        entity_types=COMPANIES_AND_LLPS,
        industries=("export", "manufacturing", "trading"),
        penalty_description=_FEMA_PENALTY,
    ),
    ObligationSpec(
        code="rbi.idpms.event",
        title="IDPMS — Submission of import bill of entry",
        regulation=Regulation.RBI,
        filing_type="IDPMS",
        frequency=Frequency.EVENT_BASED,
        section="Master Direction — Import of Goods and Services",
        description=(
            "Evidence of import must be submitted to the AD bank within six months of "
            "remittance, and the entry closed on IDPMS."
        ),
        authority=_AUTHORITY,
        offset_days=180,
        entity_types=COMPANIES_AND_LLPS,
        industries=("import", "manufacturing", "trading"),
        penalty_description=_FEMA_PENALTY,
    ),
    ObligationSpec(
        code="rbi.ppi_audit.annual",
        title="System audit report — Prepaid payment instruments",
        regulation=Regulation.RBI,
        filing_type="SAR",
        frequency=Frequency.ANNUAL,
        section="Master Direction on Prepaid Payment Instruments",
        description=(
            "Annual system audit by a CERT-In empanelled auditor, submitted to the "
            "Department of Payment and Settlement Systems within two months of the "
            "year end."
        ),
        authority=_AUTHORITY,
        due_month=5,
        due_day=31,
        entity_types=COMPANIES_AND_LLPS,
        industries=("fintech", "payments", "prepaid-instruments"),
        min_turnover_paise=5 * CRORE,
        penalty_description="Up to ₹10 lakh or twice the loss caused, under the PSS Act 2007",
        penalty_max_paise=10 * LAKH,
    ),
)
