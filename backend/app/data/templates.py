"""The system template library (section 4.8).

Two kinds of template, in one table, distinguished by ``category``:

* **Filing templates** (``category="filing"``) carry a field schema. The filing
  editor renders it, and — more importantly — the AI generator is handed it as
  the JSON contract it must fill. That is what keeps a generated draft
  structurally valid instead of free prose that somebody then has to retype.
* **Document templates** (``category="document"``) carry ``body_template``, a
  string with ``{placeholder}`` slots for board resolutions and regulatory
  replies.

**Field types** are deliberately few — ``text``, ``textarea``, ``number``,
``money``, ``date``, ``boolean``, ``select``, ``table``. A richer type system
would have to be understood by the renderer, by the validator and by the model
that fills the form, and every type added is a type each of the three can
disagree about.

Money fields are in **paise**, like every other amount in the product. The
field's ``help`` says so, because a template is the one place a human types a
number in directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.models.enums import Regulation


def _f(
    key: str,
    label: str,
    type_: str = "text",
    *,
    required: bool = False,
    help_: str | None = None,
    options: list[str] | None = None,
    columns: list[dict] | None = None,
) -> dict:
    """One field definition. A helper because there are a few hundred of them."""
    out: dict = {"key": key, "label": label, "type": type_, "required": required}
    if help_:
        out["help"] = help_
    if options:
        out["options"] = options
    if columns:
        out["columns"] = columns
    return out


@dataclass(frozen=True)
class TemplateSpec:
    """One row of the system template library."""

    code: str
    name: str
    category: str = "filing"
    description: str | None = None
    regulation: Regulation | None = None
    filing_type: str | None = None
    fields: list[dict] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)
    body_template: str | None = None
    instructions: str | None = None
    statutory_reference: str | None = None
    version: int = 1

    def to_columns(self) -> dict:
        template_json: dict | None = None
        if self.fields or self.sections:
            template_json = {"fields": self.fields}
            if self.sections:
                template_json["sections"] = self.sections
        return {
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "regulation": self.regulation,
            "filing_type": self.filing_type,
            "category": self.category,
            "template_json": template_json,
            "body_template": self.body_template,
            "version": self.version,
            "is_active": True,
            "is_system": True,
            "instructions": self.instructions,
            "statutory_reference": self.statutory_reference,
            "organization_id": None,
        }


# --- Filing templates -------------------------------------------------------

_GSTR3B = TemplateSpec(
    code="gst.gstr3b",
    name="GSTR-3B — Summary return",
    regulation=Regulation.GST,
    filing_type="GSTR-3B",
    description="Monthly or quarterly summary of supplies, input tax credit and tax payable.",
    statutory_reference="Section 39, CGST Act 2017",
    instructions=(
        "Table 3.1 is outward supplies; 4 is input tax credit; 5.1 is interest and "
        "late fee. The tax payable is 3.1 less the credit claimed in 4 — the generator "
        "computes it but does not file it, and a human must confirm before submission."
    ),
    sections=[
        {"key": "outward", "title": "3.1 Outward supplies and reverse charge"},
        {"key": "itc", "title": "4. Eligible input tax credit"},
        {"key": "payment", "title": "5.1 Interest and late fee"},
    ],
    fields=[
        _f("gstin", "GSTIN", "text", required=True),
        _f("period", "Return period", "text", required=True, help_="YYYY-MM"),
        _f(
            "outward_taxable_value",
            "Taxable value of outward supplies",
            "money",
            required=True,
            help_="In paise",
        ),
        _f("outward_igst", "IGST on outward supplies", "money", help_="In paise"),
        _f("outward_cgst", "CGST on outward supplies", "money", help_="In paise"),
        _f("outward_sgst", "SGST/UTGST on outward supplies", "money", help_="In paise"),
        _f("outward_cess", "Cess on outward supplies", "money", help_="In paise"),
        _f("zero_rated_value", "Zero-rated supplies (exports and SEZ)", "money", help_="In paise"),
        _f("exempt_value", "Exempt, nil-rated and non-GST supplies", "money", help_="In paise"),
        _f("reverse_charge_value", "Inward supplies liable to reverse charge", "money"),
        _f("itc_igst", "IGST credit availed", "money", help_="In paise"),
        _f("itc_cgst", "CGST credit availed", "money", help_="In paise"),
        _f("itc_sgst", "SGST/UTGST credit availed", "money", help_="In paise"),
        _f("itc_reversed", "Credit reversed under Rules 42 and 43", "money"),
        _f("interest_payable", "Interest payable", "money"),
        _f("late_fee_payable", "Late fee payable", "money"),
        _f("net_tax_payable", "Net tax payable in cash", "money", required=True),
    ],
)

_GSTR1 = TemplateSpec(
    code="gst.gstr1",
    name="GSTR-1 — Outward supplies",
    regulation=Regulation.GST,
    filing_type="GSTR-1",
    description="Invoice-level statement of outward supplies for the period.",
    statutory_reference="Section 37, CGST Act 2017",
    instructions=(
        "B2B supplies are reported invoice by invoice; B2C below ₹2.5 lakh are "
        "consolidated by rate and place of supply. The B2B table is what flows into "
        "the recipient's GSTR-2B, so an error here becomes the customer's problem."
    ),
    fields=[
        _f("gstin", "GSTIN", "text", required=True),
        _f("period", "Return period", "text", required=True, help_="YYYY-MM"),
        _f(
            "b2b_invoices",
            "B2B invoices",
            "table",
            columns=[
                {"key": "recipient_gstin", "label": "Recipient GSTIN", "type": "text"},
                {"key": "invoice_no", "label": "Invoice number", "type": "text"},
                {"key": "invoice_date", "label": "Invoice date", "type": "date"},
                {"key": "taxable_value", "label": "Taxable value (paise)", "type": "money"},
                {"key": "rate", "label": "Rate %", "type": "number"},
                {"key": "igst", "label": "IGST (paise)", "type": "money"},
                {"key": "cgst", "label": "CGST (paise)", "type": "money"},
                {"key": "sgst", "label": "SGST (paise)", "type": "money"},
                {"key": "place_of_supply", "label": "Place of supply", "type": "text"},
            ],
        ),
        _f(
            "b2c_summary",
            "B2C supplies (consolidated)",
            "table",
            columns=[
                {"key": "place_of_supply", "label": "Place of supply", "type": "text"},
                {"key": "rate", "label": "Rate %", "type": "number"},
                {"key": "taxable_value", "label": "Taxable value (paise)", "type": "money"},
                {"key": "tax", "label": "Tax (paise)", "type": "money"},
            ],
        ),
        _f("export_invoices", "Export invoices", "table", columns=[
            {"key": "invoice_no", "label": "Invoice number", "type": "text"},
            {"key": "invoice_date", "label": "Invoice date", "type": "date"},
            {"key": "port_code", "label": "Port code", "type": "text"},
            {"key": "shipping_bill_no", "label": "Shipping bill number", "type": "text"},
            {"key": "taxable_value", "label": "Taxable value (paise)", "type": "money"},
            {"key": "with_payment", "label": "With payment of IGST", "type": "boolean"},
        ]),
        _f("total_taxable_value", "Total taxable value", "money", required=True),
        _f("total_tax", "Total tax", "money", required=True),
    ],
)

_GSTR9 = TemplateSpec(
    code="gst.gstr9",
    name="GSTR-9 — Annual return",
    regulation=Regulation.GST,
    filing_type="GSTR-9",
    description="Annual reconciliation of the year's GSTR-1 and GSTR-3B filings.",
    statutory_reference="Section 44, CGST Act 2017",
    instructions=(
        "Every figure here should reconcile to the twelve monthly returns already "
        "filed. Where it does not, the difference belongs in the amendments table "
        "with an explanation — an unexplained difference is what triggers a notice."
    ),
    fields=[
        _f("gstin", "GSTIN", "text", required=True),
        _f("financial_year", "Financial year", "text", required=True, help_="e.g. FY2026-27"),
        _f("total_outward_taxable", "Total taxable outward supplies", "money", required=True),
        _f("total_outward_exempt", "Exempt and nil-rated outward supplies", "money"),
        _f("total_tax_paid", "Total tax paid", "money", required=True),
        _f("total_itc_availed", "Total input tax credit availed", "money", required=True),
        _f("total_itc_reversed", "Total input tax credit reversed", "money"),
        _f("demands_paid", "Demands and refunds", "money"),
        _f("late_fee_paid", "Late fee paid", "money"),
        _f(
            "differences_explanation",
            "Explanation of differences from monthly returns",
            "textarea",
        ),
    ],
)

_MGT7 = TemplateSpec(
    code="mca.mgt7",
    name="MGT-7 — Annual return",
    regulation=Regulation.MCA,
    filing_type="MGT-7",
    description="Annual return of a company under section 92 of the Companies Act 2013.",
    statutory_reference="Section 92, Companies Act 2013",
    instructions=(
        "The shareholding table must reconcile to the register of members as at the "
        "financial year end, not as at the filing date. Board and committee meeting "
        "counts come from the minute book."
    ),
    sections=[
        {"key": "identity", "title": "I. Registration and principal business"},
        {"key": "capital", "title": "IV. Share capital and shareholding"},
        {"key": "governance", "title": "V. Directors and key managerial personnel"},
        {"key": "meetings", "title": "VI. Meetings of members and the board"},
    ],
    fields=[
        _f("cin", "Corporate Identity Number", "text", required=True),
        _f("company_name", "Company name", "text", required=True),
        _f("registered_office", "Registered office address", "textarea", required=True),
        _f("financial_year", "Financial year", "text", required=True),
        _f("principal_business_activity", "Principal business activity", "text", required=True),
        _f("authorised_capital", "Authorised share capital", "money", required=True),
        _f("paid_up_capital", "Paid-up share capital", "money", required=True),
        _f(
            "shareholding",
            "Shareholding pattern",
            "table",
            columns=[
                {"key": "category", "label": "Category", "type": "text"},
                {"key": "shares", "label": "Number of shares", "type": "number"},
                {"key": "percentage", "label": "Percentage", "type": "number"},
            ],
        ),
        _f(
            "directors",
            "Directors and KMP",
            "table",
            columns=[
                {"key": "din", "label": "DIN/PAN", "type": "text"},
                {"key": "name", "label": "Name", "type": "text"},
                {"key": "designation", "label": "Designation", "type": "text"},
                {"key": "appointment_date", "label": "Date of appointment", "type": "date"},
            ],
        ),
        _f("board_meetings_held", "Number of board meetings held", "number", required=True),
        _f("agm_date", "Date of the annual general meeting", "date", required=True),
        _f("indebtedness", "Total indebtedness at year end", "money"),
        _f("penalties_details", "Penalties, punishments and compounding", "textarea"),
    ],
)

_AOC4 = TemplateSpec(
    code="mca.aoc4",
    name="AOC-4 — Financial statements",
    regulation=Regulation.MCA,
    filing_type="AOC-4",
    description="Filing of the audited financial statements with the Registrar.",
    statutory_reference="Section 137, Companies Act 2013",
    instructions=(
        "The figures must match the signed financial statements exactly. Where the "
        "auditor has qualified the report, the qualification and the board's reply "
        "both have to be attached — a filing without them is liable to be rejected."
    ),
    fields=[
        _f("cin", "Corporate Identity Number", "text", required=True),
        _f("financial_year", "Financial year", "text", required=True),
        _f("revenue_from_operations", "Revenue from operations", "money", required=True),
        _f("other_income", "Other income", "money"),
        _f("total_expenses", "Total expenses", "money", required=True),
        _f("profit_before_tax", "Profit before tax", "money", required=True),
        _f("tax_expense", "Tax expense", "money"),
        _f("profit_after_tax", "Profit after tax", "money", required=True),
        _f("total_assets", "Total assets", "money", required=True),
        _f("total_liabilities", "Total liabilities", "money", required=True),
        _f("net_worth", "Net worth", "money", required=True),
        _f("auditor_name", "Auditor name", "text", required=True),
        _f("auditor_frn", "Auditor firm registration number", "text", required=True),
        _f("audit_report_date", "Date of the audit report", "date", required=True),
        _f(
            "audit_qualification",
            "Auditor's qualification, reservation or adverse remark",
            "textarea",
        ),
        _f("agm_date", "Date of the annual general meeting", "date", required=True),
    ],
)

_INC20A = TemplateSpec(
    code="mca.inc20a",
    name="INC-20A — Declaration of commencement of business",
    regulation=Regulation.MCA,
    filing_type="INC-20A",
    description="Declaration that subscribers have paid the value of the shares agreed.",
    statutory_reference="Section 10A, Companies Act 2013",
    fields=[
        _f("cin", "Corporate Identity Number", "text", required=True),
        _f("incorporation_date", "Date of incorporation", "date", required=True),
        _f("subscribed_capital", "Total subscribed capital", "money", required=True),
        _f("amount_received", "Amount received from subscribers", "money", required=True),
        _f("bank_name", "Bank in which the money was deposited", "text", required=True),
        _f("bank_account_last4", "Account number (last four digits)", "text"),
        _f("registered_office_verified", "Registered office verification attached", "boolean"),
        _f("declaring_director_din", "DIN of the declaring director", "text", required=True),
    ],
)

_DIR3KYC = TemplateSpec(
    code="mca.dir3kyc",
    name="DIR-3 KYC — Director KYC",
    regulation=Regulation.MCA,
    filing_type="DIR-3 KYC",
    description="Annual KYC of a person holding a Director Identification Number.",
    statutory_reference="Rule 12A, Companies (Appointment and Qualification of Directors) Rules",
    fields=[
        _f("din", "Director Identification Number", "text", required=True),
        _f("full_name", "Name as per PAN", "text", required=True),
        _f("date_of_birth", "Date of birth", "date", required=True),
        _f("pan", "PAN", "text", required=True),
        _f("mobile", "Mobile number (to be OTP verified)", "text", required=True),
        _f("email", "Email (to be OTP verified)", "text", required=True),
        _f("permanent_address", "Permanent address", "textarea", required=True),
        _f("present_address", "Present address", "textarea"),
        _f("citizenship", "Citizenship", "text", required=True),
    ],
)

_ADT1 = TemplateSpec(
    code="mca.adt1",
    name="ADT-1 — Notice of auditor appointment",
    regulation=Regulation.MCA,
    filing_type="ADT-1",
    description="Intimation to the Registrar of an auditor appointed at the AGM.",
    statutory_reference="Section 139, Companies Act 2013",
    fields=[
        _f("cin", "Corporate Identity Number", "text", required=True),
        _f("auditor_name", "Auditor name", "text", required=True),
        _f("auditor_frn", "Firm registration number", "text", required=True),
        _f("auditor_pan", "Auditor PAN", "text"),
        _f("auditor_address", "Auditor address", "textarea", required=True),
        _f("appointment_date", "Date of appointment", "date", required=True),
        _f("agm_date", "Date of the AGM at which appointed", "date", required=True),
        _f("term_from_fy", "Appointed from financial year", "text", required=True),
        _f("term_to_fy", "Appointed until financial year", "text", required=True),
        _f("is_casual_vacancy", "Appointment to fill a casual vacancy", "boolean"),
    ],
)

_ITR6 = TemplateSpec(
    code="it.itr6",
    name="ITR-6 — Return of income for a company",
    regulation=Regulation.INCOME_TAX,
    filing_type="ITR-6",
    description="Company return of income under section 139(1).",
    statutory_reference="Section 139(1), Income-tax Act 1961",
    instructions=(
        "The profit before tax must tie to the AOC-4 filing for the same year. A "
        "difference between the two is the single most common trigger for a scrutiny "
        "notice, so the generator flags it rather than smoothing it over."
    ),
    fields=[
        _f("pan", "PAN", "text", required=True),
        _f("assessment_year", "Assessment year", "text", required=True),
        _f("gross_total_income", "Gross total income", "money", required=True),
        _f("deductions_chapter_via", "Deductions under Chapter VI-A", "money"),
        _f("total_income", "Total income", "money", required=True),
        _f("book_profit_115jb", "Book profit under section 115JB", "money"),
        _f("tax_on_total_income", "Tax on total income", "money", required=True),
        _f("mat_credit_utilised", "MAT credit utilised", "money"),
        _f("advance_tax_paid", "Advance tax paid", "money"),
        _f("tds_credit", "TDS and TCS credit", "money"),
        _f("self_assessment_tax", "Self-assessment tax paid", "money"),
        _f("refund_due", "Refund due", "money"),
        _f("is_115baa", "Opting for the concessional rate under 115BAA", "boolean"),
        _f("audit_under_44ab", "Accounts audited under section 44AB", "boolean"),
    ],
)

_ITR4 = TemplateSpec(
    code="it.itr4",
    name="ITR-4 (Sugam) — Presumptive income return",
    regulation=Regulation.INCOME_TAX,
    filing_type="ITR-4",
    description="Return for a resident declaring presumptive income under 44AD/44ADA/44AE.",
    statutory_reference="Section 139(1) read with section 44AD",
    fields=[
        _f("pan", "PAN", "text", required=True),
        _f("assessment_year", "Assessment year", "text", required=True),
        _f("gross_turnover", "Gross turnover or receipts", "money", required=True),
        _f("turnover_digital", "Of which received digitally", "money"),
        _f("presumptive_income", "Presumptive income declared", "money", required=True),
        _f("other_income", "Income from other sources", "money"),
        _f("deductions_chapter_via", "Deductions under Chapter VI-A", "money"),
        _f("total_income", "Total income", "money", required=True),
        _f("tax_payable", "Tax payable", "money", required=True),
        _f("advance_tax_paid", "Advance tax and TDS", "money"),
        _f("regime", "Tax regime", "select", options=["new", "old"], required=True),
    ],
)

_FLA = TemplateSpec(
    code="rbi.fla",
    name="FLA — Annual return on foreign liabilities and assets",
    regulation=Regulation.RBI,
    filing_type="FLA",
    description="Annual FLAIR return for entities with FDI or overseas investment.",
    statutory_reference="FEMA 1999, FLA Return directions",
    fields=[
        _f("pan", "PAN", "text", required=True),
        _f("cin", "CIN", "text"),
        _f("financial_year", "Financial year", "text", required=True),
        _f("accounts_audited", "Accounts audited", "boolean", required=True),
        _f("paid_up_capital", "Total paid-up capital", "money", required=True),
        _f("non_resident_holding_pct", "Non-resident holding percentage", "number", required=True),
        _f("fdi_equity_inflow", "FDI equity inflow during the year", "money"),
        _f("odi_outflow", "Overseas direct investment during the year", "money"),
        _f("total_foreign_liabilities", "Total foreign liabilities at year end", "money"),
        _f("total_foreign_assets", "Total foreign assets at year end", "money"),
        _f("exports", "Exports during the year", "money"),
        _f("imports", "Imports during the year", "money"),
    ],
)

_FCGPR = TemplateSpec(
    code="fema.fcgpr",
    name="FC-GPR — Reporting of shares issued to a non-resident",
    regulation=Regulation.FEMA,
    filing_type="FC-GPR",
    description="FIRMS filing within 30 days of allotment to a person resident outside India.",
    statutory_reference="FEM (Non-debt Instruments) Rules 2019",
    instructions=(
        "The valuation certificate and the FIRC/KYC from the AD bank must be attached. "
        "The thirty days run from the date of allotment, not from the date the money "
        "arrived — check the board resolution, not the bank statement."
    ),
    fields=[
        _f("cin", "CIN", "text", required=True),
        _f("allotment_date", "Date of allotment", "date", required=True),
        _f("instrument_type", "Instrument", "select", required=True,
           options=["equity_shares", "compulsorily_convertible_preference_shares",
                    "compulsorily_convertible_debentures", "share_warrants"]),
        _f("number_of_shares", "Number of instruments allotted", "number", required=True),
        _f("face_value", "Face value per instrument", "money", required=True),
        _f("premium", "Premium per instrument", "money"),
        _f("total_consideration", "Total consideration received", "money", required=True),
        _f("investor_name", "Investor name", "text", required=True),
        _f("investor_country", "Investor country of residence", "text", required=True),
        _f("fdi_route", "Route", "select", options=["automatic", "government"], required=True),
        _f("nic_code", "NIC code of the activity", "text", required=True),
        _f("valuation_certificate_date", "Date of the valuation certificate", "date",
           required=True),
        _f("firc_reference", "FIRC reference", "text"),
    ],
)

_SHP = TemplateSpec(
    code="sebi.shareholding",
    name="Shareholding pattern (Regulation 31)",
    regulation=Regulation.SEBI,
    filing_type="Reg 31 SHP",
    description="Quarterly shareholding pattern filed with the stock exchange.",
    statutory_reference="Regulation 31, SEBI (LODR) Regulations 2015",
    fields=[
        _f("isin", "ISIN", "text", required=True),
        _f("quarter_ended", "Quarter ended", "date", required=True),
        _f("total_shares", "Total number of shares", "number", required=True),
        _f("promoter_shares", "Shares held by the promoter group", "number", required=True),
        _f("promoter_pct", "Promoter holding percentage", "number", required=True),
        _f("public_shares", "Shares held by the public", "number", required=True),
        _f("pledged_shares", "Shares pledged or otherwise encumbered", "number"),
        _f("dematerialised_shares", "Shares held in dematerialised form", "number", required=True),
        _f("foreign_holding_pct", "Foreign holding percentage", "number"),
    ],
)

_PF_ECR = TemplateSpec(
    code="labor.pf_ecr",
    name="PF — Electronic Challan cum Return",
    regulation=Regulation.LABOR,
    filing_type="ECR",
    description="Monthly provident fund contribution return.",
    statutory_reference="Employees' Provident Funds Scheme 1952",
    fields=[
        _f("establishment_code", "Establishment code", "text", required=True),
        _f("wage_month", "Wage month", "text", required=True, help_="YYYY-MM"),
        _f("employee_count", "Number of contributing members", "number", required=True),
        _f("total_wages", "Total EPF wages", "money", required=True),
        _f("employee_contribution", "Employee share (12%)", "money", required=True),
        _f("employer_epf_contribution", "Employer EPF share (3.67%)", "money", required=True),
        _f("employer_eps_contribution", "Employer EPS share (8.33%)", "money", required=True),
        _f("edli_contribution", "EDLI contribution (0.50%)", "money"),
        _f("admin_charges", "Administrative charges", "money"),
        _f("total_remittance", "Total remittance", "money", required=True),
    ],
)

_DPDP_BREACH = TemplateSpec(
    code="dpdp.breach_notification",
    name="DPDP — Breach notification to the Data Protection Board",
    regulation=Regulation.DPDP,
    filing_type="Breach Notification",
    description="Intimation of a personal data breach under section 8(6).",
    statutory_reference="Section 8(6), Digital Personal Data Protection Act 2023",
    instructions=(
        "File within 72 hours of detection. An incomplete notification filed on time "
        "is better than a complete one filed late — the Act penalises the failure to "
        "notify, and further particulars can follow."
    ),
    fields=[
        _f("incident_reference", "Internal incident reference", "text", required=True),
        _f("detected_at", "Date and time of detection", "date", required=True),
        _f("occurred_at", "Estimated date of occurrence", "date"),
        _f("nature_of_breach", "Nature and circumstances of the breach", "textarea",
           required=True),
        _f("data_categories", "Categories of personal data affected", "textarea", required=True),
        _f("principals_affected", "Estimated number of data principals affected", "number",
           required=True),
        _f("likely_consequences", "Likely consequences for data principals", "textarea",
           required=True),
        _f("mitigation_taken", "Measures taken to mitigate harm", "textarea", required=True),
        _f("containment_status", "Containment status", "select", required=True,
           options=["contained", "ongoing", "under_investigation"]),
        _f("principals_notified", "Have affected principals been notified", "boolean",
           required=True),
        _f("contact_person", "Contact person for the Board", "text", required=True),
        _f("contact_email", "Contact email", "text", required=True),
    ],
)

_DPDP_CONSENT = TemplateSpec(
    code="dpdp.consent_notice",
    name="DPDP — Consent notice",
    category="document",
    regulation=Regulation.DPDP,
    description="The notice served to a data principal when consent is sought.",
    statutory_reference="Section 5, Digital Personal Data Protection Act 2023",
    instructions=(
        "The Act requires the notice be available in English and in the Eighth "
        "Schedule languages. Serve it in the language the principal chose and record "
        "which one, because proving the consent was informed means proving they could "
        "read it."
    ),
    body_template=(
        "NOTICE UNDER SECTION 5 OF THE DIGITAL PERSONAL DATA PROTECTION ACT, 2023\n\n"
        "{organization_name} (\"we\") is the data fiduciary for the personal data "
        "described below.\n\n"
        "PERSONAL DATA WE COLLECT\n{data_categories}\n\n"
        "PURPOSE\nWe will process this personal data for the following purpose, and "
        "for no other:\n{purpose_description}\n\n"
        "YOUR RIGHTS\nYou may at any time:\n"
        "  - ask us for a summary of the personal data we hold about you and how we "
        "process it (section 11);\n"
        "  - ask us to correct, complete or update it (section 12);\n"
        "  - ask us to erase it, where it is no longer needed for the purpose above "
        "and no law requires us to keep it (section 12);\n"
        "  - nominate another person to exercise these rights on your behalf in the "
        "event of your death or incapacity (section 14);\n"
        "  - withdraw this consent, with the same ease with which you gave it "
        "(section 6(4)).\n\n"
        "Withdrawing consent does not affect processing already carried out lawfully "
        "before the withdrawal.\n\n"
        "GRIEVANCES\nOur Data Protection Officer can be reached at {dpo_contact}. If "
        "you are not satisfied with our response you may complain to the Data "
        "Protection Board of India.\n\n"
        "Notice version {notice_version}, dated {notice_date}."
    ),
)

# --- Document templates ------------------------------------------------------

_BOARD_RESOLUTION = TemplateSpec(
    code="doc.board_resolution",
    name="Board resolution — General form",
    category="document",
    regulation=Regulation.MCA,
    description="Certified extract of a board resolution, in the form registrars expect.",
    statutory_reference="Section 179, Companies Act 2013",
    body_template=(
        "CERTIFIED TRUE COPY OF THE RESOLUTION PASSED AT THE MEETING OF THE BOARD OF "
        "DIRECTORS OF {company_name} HELD ON {meeting_date} AT {meeting_time} AT "
        "{meeting_venue}\n\n"
        "\"RESOLVED THAT {resolution_text}\n\n"
        "RESOLVED FURTHER THAT {authorised_person}, {authorised_designation} of the "
        "Company, be and is hereby authorised to do all such acts, deeds and things "
        "as may be necessary to give effect to the above resolution, including "
        "signing and filing the requisite forms with the Registrar of Companies.\"\n\n"
        "For {company_name}\n\n\n"
        "{signatory_name}\n{signatory_designation}\nDIN: {signatory_din}\n"
        "Date: {resolution_date}\nPlace: {place}"
    ),
)

_NOTICE_REPLY = TemplateSpec(
    code="doc.notice_reply",
    name="Reply to a show-cause notice",
    category="document",
    description="Structured reply to a departmental show-cause notice.",
    instructions=(
        "Answer every allegation separately and in the order the notice makes them. A "
        "reply that argues the general position without addressing paragraph 3 leaves "
        "paragraph 3 admitted."
    ),
    body_template=(
        "To,\n{addressee}\n{department}\n{department_address}\n\n"
        "Subject: Reply to show-cause notice {notice_reference} dated {notice_date}\n\n"
        "Sir/Madam,\n\n"
        "1. We are in receipt of the notice referenced above, which was served on "
        "{service_date}. We respectfully submit our reply below within the time "
        "allowed.\n\n"
        "2. PRELIMINARY SUBMISSIONS\n{preliminary_submissions}\n\n"
        "3. REPLY TO THE SPECIFIC ALLEGATIONS\n{allegation_wise_reply}\n\n"
        "4. DOCUMENTS RELIED UPON\n{documents_list}\n\n"
        "5. PRAYER\nIn the light of the above, we respectfully pray that the notice be "
        "dropped and the proceedings closed. We further request an opportunity of "
        "personal hearing before any adverse order is passed.\n\n"
        "Yours faithfully,\nFor {organization_name}\n\n\n"
        "{signatory_name}\n{signatory_designation}\n"
        "GSTIN/PAN: {identifier}\nDate: {reply_date}\nPlace: {place}"
    ),
)

_COMPLIANCE_CERTIFICATE = TemplateSpec(
    code="doc.compliance_certificate",
    name="Annual compliance certificate",
    category="document",
    description="Certificate of compliance for the board's records and the annual report.",
    body_template=(
        "COMPLIANCE CERTIFICATE\n\n"
        "To the Board of Directors of {company_name}\n\n"
        "I/We have examined the compliance by the Company with the applicable "
        "provisions of the statutes listed below for the financial year "
        "{financial_year}, and report as follows.\n\n"
        "STATUTES EXAMINED\n{statutes_examined}\n\n"
        "FILINGS MADE DURING THE YEAR\n{filings_summary}\n\n"
        "INSTANCES OF NON-COMPLIANCE\n{non_compliance}\n\n"
        "OBSERVATIONS AND RECOMMENDATIONS\n{observations}\n\n"
        "This certificate is issued on the basis of the records produced to us and the "
        "representations made by the management, and is neither an assurance as to the "
        "future viability of the Company nor an audit of its financial statements.\n\n"
        "{certifier_name}\n{certifier_designation}\n"
        "Membership/FRN: {certifier_registration}\n"
        "Date: {certificate_date}\nPlace: {place}"
    ),
)

_PIA = TemplateSpec(
    code="dpdp.pia",
    name="Privacy Impact Assessment",
    regulation=Regulation.DPDP,
    filing_type="PIA",
    description="Guided privacy impact assessment questionnaire, scored by the risk engine.",
    statutory_reference="Section 10(2)(c), Digital Personal Data Protection Act 2023",
    instructions=(
        "Answer for the processing activity as it actually runs, not as the policy "
        "describes it. The risk score is computed from these answers, and a score "
        "derived from an aspirational description is worth nothing at an inquiry."
    ),
    sections=[
        {"key": "activity", "title": "1. The processing activity"},
        {"key": "data", "title": "2. Data and data principals"},
        {"key": "risk", "title": "3. Risk factors"},
        {"key": "controls", "title": "4. Controls and mitigations"},
    ],
    fields=[
        _f("activity_name", "Name of the processing activity", "text", required=True),
        _f("activity_description", "What the activity does", "textarea", required=True),
        _f("legal_basis", "Legal basis", "select", required=True,
           options=["consent", "legitimate_use", "employment", "legal_obligation"]),
        _f("data_categories", "Categories of personal data", "textarea", required=True),
        _f("involves_financial_data", "Involves financial data", "boolean", required=True),
        _f("involves_health_data", "Involves health data", "boolean", required=True),
        _f("involves_biometric_data", "Involves biometric data", "boolean", required=True),
        _f("involves_children", "Involves data of children under 18", "boolean", required=True),
        _f("principal_count", "Approximate number of data principals", "number", required=True),
        _f("retention_period_days", "Retention period in days", "number", required=True),
        _f("automated_decision_making", "Involves automated decision-making", "boolean",
           required=True),
        _f("cross_border_transfer", "Involves transfer outside India", "boolean", required=True),
        _f("transfer_countries", "Countries data is transferred to", "text"),
        _f("processors", "Third-party processors involved", "textarea"),
        _f("encryption_at_rest", "Data encrypted at rest", "boolean", required=True),
        _f("encryption_in_transit", "Data encrypted in transit", "boolean", required=True),
        _f("access_controls", "Access control measures", "textarea", required=True),
        _f("breach_response_plan", "Documented breach response plan exists", "boolean",
           required=True),
        _f("dpo_consulted", "Data Protection Officer consulted", "boolean"),
    ],
)


SYSTEM_TEMPLATES: tuple[TemplateSpec, ...] = (
    _GSTR3B,
    _GSTR1,
    _GSTR9,
    _MGT7,
    _AOC4,
    _INC20A,
    _DIR3KYC,
    _ADT1,
    _ITR6,
    _ITR4,
    _FLA,
    _FCGPR,
    _SHP,
    _PF_ECR,
    _DPDP_BREACH,
    _DPDP_CONSENT,
    _PIA,
    _BOARD_RESOLUTION,
    _NOTICE_REPLY,
    _COMPLIANCE_CERTIFICATE,
)

TEMPLATES_BY_CODE: dict[str, TemplateSpec] = {t.code: t for t in SYSTEM_TEMPLATES}

if len(TEMPLATES_BY_CODE) != len(SYSTEM_TEMPLATES):  # pragma: no cover - import-time guard
    raise ValueError("Duplicate template code in the system library")
