"""Income Tax obligations: returns, TDS/TCS, advance tax and the audit reports.

**Advance tax is modelled as four annual obligations, not one quarterly one.**
Each instalment has its own statutory date (15 June, 15 September, 15 December,
15 March) and its own cumulative percentage, and all four fall *inside* the year
being reported on rather than after it. A quarterly frequency with a single rule
cannot express that; four annual rows with ``period_offset=0`` express it
exactly, and each carries the percentage in its title where a user will see it.

**A known approximation.** The quarterly TDS returns are due on the last day of
the month following the quarter — except Q4, which the statute extends to 31
May. The engine's rules cannot say "the following month, but two months for the
fourth quarter", so Q4 is generated with an April due date. That warns a month
early, never late, and the row's ``metadata`` says so.
"""
from __future__ import annotations

from app.data.catalogue.base import (
    COMPANIES,
    COMPANIES_AND_LLPS,
    CRORE,
    GST_REGISTRABLE,
    LAKH,
    RUPEE,
    ObligationSpec,
)
from app.models.enums import EntityType, Frequency, Regulation

_AUTHORITY = "Central Board of Direct Taxes (CBDT)"

# Section 234F: the fee for filing a return after the due date.
_LATE_RETURN_FEE = 5_000 * RUPEE

_TDS_Q4_NOTE = {
    "approximation": (
        "The statutory due date for the January–March quarter is 31 May. The "
        "deadline engine expresses 'the last day of the following month', which "
        "yields 30 April for that quarter — a month early. Early is the safe "
        "direction; do not 'correct' this by moving the rule, which would make "
        "the other three quarters a month late."
    )
}

INCOME_TAX_OBLIGATIONS: tuple[ObligationSpec, ...] = (
    # --- Returns of income -------------------------------------------------
    ObligationSpec(
        code="it.itr4.annual",
        title="ITR-4 (Sugam) — Presumptive income return",
        regulation=Regulation.INCOME_TAX,
        filing_type="ITR-4",
        frequency=Frequency.ANNUAL,
        section="Section 139(1) read with sections 44AD/44ADA/44AE",
        description=(
            "Return for a resident proprietorship, HUF or partnership declaring "
            "income on a presumptive basis. Due 31 July where no audit is required."
        ),
        authority=_AUTHORITY,
        due_month=7,
        due_day=31,
        entity_types=(
            EntityType.PROPRIETORSHIP,
            EntityType.PARTNERSHIP,
            EntityType.HUF,
        ),
        max_turnover_paise=2 * CRORE,
        penalty_description="₹5,000 under section 234F, plus 1% per month interest under 234A",
        penalty_max_paise=_LATE_RETURN_FEE,
    ),
    ObligationSpec(
        code="it.itr5.annual",
        title="ITR-5 — Return for firms, LLPs, AOPs and BOIs",
        regulation=Regulation.INCOME_TAX,
        filing_type="ITR-5",
        frequency=Frequency.ANNUAL,
        section="Section 139(1), Income-tax Act 1961",
        description="Return of income for a partnership firm, LLP, AOP, BOI or trust.",
        authority=_AUTHORITY,
        due_month=7,
        due_day=31,
        entity_types=(
            EntityType.LLP,
            EntityType.PARTNERSHIP,
            EntityType.TRUST,
            EntityType.SOCIETY,
        ),
        penalty_description="₹5,000 under section 234F, plus interest under 234A",
        penalty_max_paise=_LATE_RETURN_FEE,
    ),
    ObligationSpec(
        code="it.itr6.annual",
        title="ITR-6 — Return for companies",
        regulation=Regulation.INCOME_TAX,
        filing_type="ITR-6",
        frequency=Frequency.ANNUAL,
        section="Section 139(1), Income-tax Act 1961",
        description=(
            "Return of income for a company other than one claiming exemption under "
            "section 11. Due 31 October where a tax audit applies, which it does for "
            "every company carrying on business."
        ),
        authority=_AUTHORITY,
        due_month=10,
        due_day=31,
        entity_types=COMPANIES,
        penalty_description="₹5,000 under section 234F, plus interest under 234A",
        penalty_max_paise=_LATE_RETURN_FEE,
    ),
    ObligationSpec(
        code="it.itr7.annual",
        title="ITR-7 — Return for trusts and political parties",
        regulation=Regulation.INCOME_TAX,
        filing_type="ITR-7",
        frequency=Frequency.ANNUAL,
        section="Sections 139(4A) to 139(4D), Income-tax Act 1961",
        description="Return for entities claiming exemption under sections 11, 12 or 10(23C).",
        authority=_AUTHORITY,
        due_month=10,
        due_day=31,
        entity_types=(EntityType.TRUST, EntityType.SOCIETY, EntityType.SECTION_8),
        penalty_description="₹5,000 under section 234F; loss of exemption in a bad case",
        penalty_max_paise=_LATE_RETURN_FEE,
    ),
    ObligationSpec(
        code="it.itr_revised.annual",
        title="Revised or belated return",
        regulation=Regulation.INCOME_TAX,
        filing_type="ITR-Revised",
        frequency=Frequency.ANNUAL,
        section="Sections 139(4) and 139(5), Income-tax Act 1961",
        description=(
            "Last date to file a belated return or revise one already filed, for the "
            "assessment year. After this the only route is an updated return under "
            "139(8A), with additional tax."
        ),
        authority=_AUTHORITY,
        due_month=12,
        due_day=31,
        entity_types=GST_REGISTRABLE,
        penalty_description="₹5,000 under section 234F for a belated return",
        penalty_max_paise=_LATE_RETURN_FEE,
    ),
    # --- Audit reports -------------------------------------------------------
    ObligationSpec(
        code="it.tax_audit.annual",
        title="Form 3CA/3CB-3CD — Tax audit report",
        regulation=Regulation.INCOME_TAX,
        filing_type="3CD",
        frequency=Frequency.ANNUAL,
        section="Section 44AB, Income-tax Act 1961",
        description=(
            "Audit report by a Chartered Accountant, required above ₹1 crore turnover "
            "for a business (₹10 crore where cash receipts and payments are each under "
            "5%) or ₹50 lakh for a profession. Due one month before the return."
        ),
        authority=_AUTHORITY,
        due_month=9,
        due_day=30,
        entity_types=GST_REGISTRABLE,
        min_turnover_paise=1 * CRORE,
        penalty_description="0.5% of turnover up to ₹1,50,000 under section 271B",
        penalty_max_paise=150_000 * RUPEE,
    ),
    ObligationSpec(
        code="it.transfer_pricing.annual",
        title="Form 3CEB — Transfer pricing report",
        regulation=Regulation.INCOME_TAX,
        filing_type="3CEB",
        frequency=Frequency.ANNUAL,
        section="Section 92E, Income-tax Act 1961",
        description=(
            "Accountant's report on international and specified domestic transactions "
            "with associated enterprises."
        ),
        authority=_AUTHORITY,
        due_month=10,
        due_day=31,
        entity_types=COMPANIES_AND_LLPS,
        requires_foreign_investment=True,
        penalty_description="₹1,00,000 under section 271BA",
        penalty_max_paise=100_000 * RUPEE,
    ),
    # --- Advance tax: four instalments, four rows --------------------------
    ObligationSpec(
        code="it.advance_tax.q1",
        title="Advance tax — 1st instalment (15% of liability)",
        regulation=Regulation.INCOME_TAX,
        filing_type="Challan 280",
        frequency=Frequency.ANNUAL,
        section="Section 211, Income-tax Act 1961",
        description="At least 15% of the year's estimated tax, paid by 15 June.",
        authority=_AUTHORITY,
        due_month=6,
        due_day=15,
        period_offset=0,
        entity_types=GST_REGISTRABLE,
        penalty_description="Interest at 1% per month on the shortfall under section 234C",
    ),
    ObligationSpec(
        code="it.advance_tax.q2",
        title="Advance tax — 2nd instalment (45% cumulative)",
        regulation=Regulation.INCOME_TAX,
        filing_type="Challan 280",
        frequency=Frequency.ANNUAL,
        section="Section 211, Income-tax Act 1961",
        description="Cumulative 45% of the year's estimated tax, paid by 15 September.",
        authority=_AUTHORITY,
        due_month=9,
        due_day=15,
        period_offset=0,
        entity_types=GST_REGISTRABLE,
        penalty_description="Interest at 1% per month on the shortfall under section 234C",
    ),
    ObligationSpec(
        code="it.advance_tax.q3",
        title="Advance tax — 3rd instalment (75% cumulative)",
        regulation=Regulation.INCOME_TAX,
        filing_type="Challan 280",
        frequency=Frequency.ANNUAL,
        section="Section 211, Income-tax Act 1961",
        description="Cumulative 75% of the year's estimated tax, paid by 15 December.",
        authority=_AUTHORITY,
        due_month=12,
        due_day=15,
        period_offset=0,
        entity_types=GST_REGISTRABLE,
        penalty_description="Interest at 1% per month on the shortfall under section 234C",
    ),
    ObligationSpec(
        code="it.advance_tax.q4",
        title="Advance tax — 4th instalment (100% cumulative)",
        regulation=Regulation.INCOME_TAX,
        filing_type="Challan 280",
        frequency=Frequency.ANNUAL,
        section="Section 211, Income-tax Act 1961",
        description=(
            "The whole of the year's estimated tax, paid by 15 March. Anything unpaid "
            "after this date attracts interest under section 234B as well as 234C."
        ),
        authority=_AUTHORITY,
        due_month=3,
        due_day=15,
        period_offset=0,
        entity_types=GST_REGISTRABLE,
        penalty_description="Interest at 1% per month under sections 234B and 234C",
    ),
    # --- TDS and TCS ---------------------------------------------------------
    ObligationSpec(
        code="it.tds_payment.monthly",
        title="TDS deposit — Monthly challan",
        regulation=Regulation.INCOME_TAX,
        filing_type="Challan 281",
        frequency=Frequency.MONTHLY,
        section="Rule 30, Income-tax Rules 1962",
        description=(
            "Tax deducted during a month must be deposited by the 7th of the next. "
            "March's deduction is the exception, due 30 April."
        ),
        authority=_AUTHORITY,
        due_day=7,
        entity_types=GST_REGISTRABLE,
        penalty_description=(
            "Interest at 1.5% per month from deduction to deposit, and disallowance of "
            "the underlying expenditure under section 40(a)(ia)"
        ),
    ),
    ObligationSpec(
        code="it.tds_24q.quarterly",
        title="Form 24Q — TDS return for salaries",
        regulation=Regulation.INCOME_TAX,
        filing_type="24Q",
        frequency=Frequency.QUARTERLY,
        section="Section 200(3) read with Rule 31A",
        description="Quarterly statement of tax deducted from salary payments.",
        authority=_AUTHORITY,
        due_day=31,
        entity_types=GST_REGISTRABLE,
        min_employees=1,
        penalty_description="₹200 per day under section 234E, capped at the tax deducted",
        penalty_per_day_paise=200 * RUPEE,
        metadata=_TDS_Q4_NOTE,
    ),
    ObligationSpec(
        code="it.tds_26q.quarterly",
        title="Form 26Q — TDS return for payments other than salary",
        regulation=Regulation.INCOME_TAX,
        filing_type="26Q",
        frequency=Frequency.QUARTERLY,
        section="Section 200(3) read with Rule 31A",
        description=(
            "Quarterly statement of tax deducted on contractor payments, professional "
            "fees, rent, interest and commission."
        ),
        authority=_AUTHORITY,
        due_day=31,
        entity_types=GST_REGISTRABLE,
        penalty_description="₹200 per day under section 234E, capped at the tax deducted",
        penalty_per_day_paise=200 * RUPEE,
        metadata=_TDS_Q4_NOTE,
    ),
    ObligationSpec(
        code="it.tds_27q.quarterly",
        title="Form 27Q — TDS return for payments to non-residents",
        regulation=Regulation.INCOME_TAX,
        filing_type="27Q",
        frequency=Frequency.QUARTERLY,
        section="Section 200(3) read with Rule 31A",
        description="Quarterly statement of tax deducted on payments to non-residents.",
        authority=_AUTHORITY,
        due_day=31,
        entity_types=GST_REGISTRABLE,
        requires_foreign_investment=True,
        penalty_description="₹200 per day under section 234E",
        penalty_per_day_paise=200 * RUPEE,
        metadata=_TDS_Q4_NOTE,
    ),
    ObligationSpec(
        code="it.tcs_27eq.quarterly",
        title="Form 27EQ — TCS return",
        regulation=Regulation.INCOME_TAX,
        filing_type="27EQ",
        frequency=Frequency.QUARTERLY,
        section="Section 206C read with Rule 31AA",
        description=(
            "Quarterly statement of tax collected at source. Due the 15th of the month "
            "following the quarter — earlier than the TDS returns."
        ),
        authority=_AUTHORITY,
        due_day=15,
        entity_types=GST_REGISTRABLE,
        penalty_description="₹200 per day under section 234E",
        penalty_per_day_paise=200 * RUPEE,
    ),
    ObligationSpec(
        code="it.form16.annual",
        title="Form 16 — TDS certificate to employees",
        regulation=Regulation.INCOME_TAX,
        filing_type="Form 16",
        frequency=Frequency.ANNUAL,
        section="Rule 31(1)(a), Income-tax Rules 1962",
        description="Salary TDS certificate, issued to every employee by 15 June.",
        authority=_AUTHORITY,
        due_month=6,
        due_day=15,
        entity_types=GST_REGISTRABLE,
        min_employees=1,
        penalty_description="₹100 per day per certificate under section 272A(2)(g)",
        penalty_per_day_paise=100 * RUPEE,
    ),
    ObligationSpec(
        code="it.form16a.quarterly",
        title="Form 16A — TDS certificate for non-salary payments",
        regulation=Regulation.INCOME_TAX,
        filing_type="Form 16A",
        frequency=Frequency.QUARTERLY,
        section="Rule 31(1)(b), Income-tax Rules 1962",
        description="Issued within 15 days of the quarterly TDS return's due date.",
        authority=_AUTHORITY,
        offset_days=45,
        entity_types=GST_REGISTRABLE,
        penalty_description="₹100 per day per certificate under section 272A(2)(g)",
        penalty_per_day_paise=100 * RUPEE,
    ),
    ObligationSpec(
        code="it.form26qb.event",
        title="Form 26QB — TDS on purchase of immovable property",
        regulation=Regulation.INCOME_TAX,
        filing_type="26QB",
        frequency=Frequency.EVENT_BASED,
        section="Section 194-IA, Income-tax Act 1961",
        description=(
            "Challan-cum-statement for 1% TDS on a property purchase above ₹50 lakh, "
            "due within 30 days of the end of the month of payment."
        ),
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=GST_REGISTRABLE,
        penalty_description="₹200 per day under section 234E plus interest",
        penalty_per_day_paise=200 * RUPEE,
    ),
    ObligationSpec(
        code="it.form61a.annual",
        title="Form 61A — Statement of financial transactions",
        regulation=Regulation.INCOME_TAX,
        filing_type="61A",
        frequency=Frequency.ANNUAL,
        section="Section 285BA read with Rule 114E",
        description=(
            "Report of specified high-value transactions — cash deposits, large "
            "receipts, securities dealings — filed by 31 May."
        ),
        authority=_AUTHORITY,
        due_month=5,
        due_day=31,
        entity_types=GST_REGISTRABLE,
        min_turnover_paise=10 * CRORE,
        penalty_description="₹500 per day under section 271FA, rising to ₹1,000 after notice",
        penalty_per_day_paise=500 * RUPEE,
    ),
    ObligationSpec(
        code="it.form10b.annual",
        title="Form 10B/10BB — Audit report for a charitable trust",
        regulation=Regulation.INCOME_TAX,
        filing_type="10B",
        frequency=Frequency.ANNUAL,
        section="Section 12A(1)(b), Income-tax Act 1961",
        description=(
            "Audit report a registered trust must file one month before its return, "
            "without which the section 11 exemption is not available."
        ),
        authority=_AUTHORITY,
        due_month=9,
        due_day=30,
        entity_types=(EntityType.TRUST, EntityType.SOCIETY, EntityType.SECTION_8),
        penalty_description="Loss of exemption under section 11 for the year",
    ),
    ObligationSpec(
        code="it.equalisation_levy.annual",
        title="Form 1 — Equalisation levy statement",
        regulation=Regulation.INCOME_TAX,
        filing_type="EL Form 1",
        frequency=Frequency.ANNUAL,
        section="Section 167, Finance Act 2016",
        description=(
            "Annual statement of the levy on specified digital advertising payments to "
            "non-residents, due 30 June."
        ),
        authority=_AUTHORITY,
        due_month=6,
        due_day=30,
        entity_types=COMPANIES_AND_LLPS,
        min_turnover_paise=100 * LAKH,
        penalty_description="₹100 per day under section 172 of the Finance Act 2016",
        penalty_per_day_paise=100 * RUPEE,
    ),
)
