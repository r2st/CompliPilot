"""Labour law obligations — PF, ESI, professional tax, and the state registrations.

The awkward family, for two reasons.

**Employee-count thresholds are the applicability rule, not turnover.** PF binds
at 20 employees, ESI at 10 (20 in a few states), the Payment of Gratuity Act at
10. Those are the ``min_employees`` values below, and they are the reason the
organization profile carries an employee count as a first-class column.

**Professional tax is a state subject and the rates and dates differ by state.**
Rather than pretend there is one national rule, the professional tax rows below
are scoped with ``states`` to the states that levy it, and the due day is the
most common one in that state's rules. A CA firm that needs a different day for
a particular client sets ``due_day_override`` on that client's
``OrganizationObligation`` — which is exactly what that column is for.
"""
from __future__ import annotations

from app.data.catalogue.base import EMPLOYERS, LAKH, RUPEE, ObligationSpec
from app.models.enums import Frequency, Regulation

_PF_AUTHORITY = "Employees' Provident Fund Organisation (EPFO)"
_ESI_AUTHORITY = "Employees' State Insurance Corporation (ESIC)"
_STATE_AUTHORITY = "State Labour Department"

# The states that levy professional tax. Not all of India does — Delhi, Haryana,
# Uttar Pradesh and Rajasthan among others do not — and putting a professional
# tax return on a Delhi client's calendar is the kind of false positive that
# costs the product its credibility.
_PT_STATES: tuple[str, ...] = (
    "Andhra Pradesh",
    "Assam",
    "Bihar",
    "Chhattisgarh",
    "Gujarat",
    "Jharkhand",
    "Karnataka",
    "Kerala",
    "Madhya Pradesh",
    "Maharashtra",
    "Manipur",
    "Meghalaya",
    "Mizoram",
    "Nagaland",
    "Odisha",
    "Puducherry",
    "Punjab",
    "Sikkim",
    "Tamil Nadu",
    "Telangana",
    "Tripura",
    "West Bengal",
)

LABOR_OBLIGATIONS: tuple[ObligationSpec, ...] = (
    # --- Provident fund ------------------------------------------------------
    ObligationSpec(
        code="labor.pf_ecr.monthly",
        title="PF — Electronic Challan cum Return",
        regulation=Regulation.LABOR,
        filing_type="ECR",
        frequency=Frequency.MONTHLY,
        section="Paragraph 38, Employees' Provident Funds Scheme 1952",
        description=(
            "Monthly contribution and return, due the 15th of the following month. "
            "The employee's share is money held in trust, so a late deposit is treated "
            "far more seriously than an ordinary tax delay."
        ),
        authority=_PF_AUTHORITY,
        due_day=15,
        entity_types=EMPLOYERS,
        min_employees=20,
        penalty_description=(
            "Interest at 12% per annum under section 7Q, plus damages of 5% to 25% per "
            "annum under section 14B depending on the length of the delay"
        ),
    ),
    ObligationSpec(
        code="labor.pf_annual_return.annual",
        title="PF — Annual return (Form 3A and 6A)",
        regulation=Regulation.LABOR,
        filing_type="Form 6A",
        frequency=Frequency.ANNUAL,
        section="Paragraph 43, Employees' Provident Funds Scheme 1952",
        description="Consolidated annual statement of contributions, due 30 April.",
        authority=_PF_AUTHORITY,
        due_month=4,
        due_day=30,
        entity_types=EMPLOYERS,
        min_employees=20,
        penalty_description="Damages under section 14B and prosecution under section 14",
    ),
    ObligationSpec(
        code="labor.pf_uan_kyc.event",
        title="PF — UAN generation and KYC for a new joiner",
        regulation=Regulation.LABOR,
        filing_type="Form 11",
        frequency=Frequency.EVENT_BASED,
        section="Paragraph 34, Employees' Provident Funds Scheme 1952",
        description=(
            "A new employee's UAN must be generated and linked, and Form 11 collected, "
            "within 15 days of joining."
        ),
        authority=_PF_AUTHORITY,
        offset_days=15,
        entity_types=EMPLOYERS,
        min_employees=20,
        penalty_description="Damages under section 14B on any resulting contribution delay",
    ),
    # --- Employees' State Insurance ------------------------------------------
    ObligationSpec(
        code="labor.esi_contribution.monthly",
        title="ESI — Monthly contribution",
        regulation=Regulation.LABOR,
        filing_type="ESI Challan",
        frequency=Frequency.MONTHLY,
        section="Regulation 31, Employees' State Insurance (General) Regulations 1950",
        description=(
            "Monthly contribution for employees earning up to ₹21,000, due the 15th of "
            "the following month."
        ),
        authority=_ESI_AUTHORITY,
        due_day=15,
        entity_types=EMPLOYERS,
        min_employees=10,
        penalty_description=(
            "Interest at 12% per annum plus damages of 5% to 25% under regulation 31C"
        ),
    ),
    ObligationSpec(
        code="labor.esi_return.half_yearly",
        title="ESI — Half-yearly return of contributions",
        regulation=Regulation.LABOR,
        filing_type="ESI RC",
        frequency=Frequency.HALF_YEARLY,
        section="Regulation 26, Employees' State Insurance (General) Regulations 1950",
        description=(
            "Return of contributions for the April–September and October–March "
            "contribution periods, due within 42 days of each period ending."
        ),
        authority=_ESI_AUTHORITY,
        offset_days=42,
        entity_types=EMPLOYERS,
        min_employees=10,
        penalty_description="Up to ₹5,000 and prosecution under section 85",
        penalty_max_paise=5_000 * RUPEE,
    ),
    ObligationSpec(
        code="labor.esi_accident.event",
        title="ESI — Accident report",
        regulation=Regulation.LABOR,
        filing_type="Form 12",
        frequency=Frequency.EVENT_BASED,
        section="Regulation 68, Employees' State Insurance (General) Regulations 1950",
        description=(
            "An employment injury must be reported to the local ESI office within 24 "
            "hours, and immediately where it is fatal or serious."
        ),
        authority=_ESI_AUTHORITY,
        offset_days=1,
        entity_types=EMPLOYERS,
        min_employees=10,
        penalty_description="Prosecution under section 85 of the ESI Act 1948",
    ),
    # --- Professional tax ------------------------------------------------------
    ObligationSpec(
        code="labor.professional_tax.monthly",
        title="Professional tax — Monthly deduction and return",
        regulation=Regulation.LABOR,
        filing_type="PT Return",
        frequency=Frequency.MONTHLY,
        section="State professional tax legislation (Article 276, Constitution of India)",
        description=(
            "Monthly deduction from salaries and payment to the state. Levied in 22 "
            "states and union territories; the due day varies by state and defaults "
            "here to the 20th, which can be overridden per client."
        ),
        authority=_STATE_AUTHORITY,
        due_day=20,
        entity_types=EMPLOYERS,
        states=_PT_STATES,
        min_employees=1,
        penalty_description="Interest and penalty per the levying state's Act, commonly 1.25% "
        "per month plus a fixed penalty",
    ),
    ObligationSpec(
        code="labor.professional_tax_enrolment.annual",
        title="Professional tax — Annual enrolment certificate payment",
        regulation=Regulation.LABOR,
        filing_type="PTEC",
        frequency=Frequency.ANNUAL,
        section="State professional tax legislation",
        description=(
            "The entity's own annual professional tax under its enrolment certificate, "
            "commonly due 30 June."
        ),
        authority=_STATE_AUTHORITY,
        due_month=6,
        due_day=30,
        period_offset=0,
        entity_types=EMPLOYERS,
        states=_PT_STATES,
        penalty_description="Interest and penalty per the levying state's Act",
    ),
    # --- Shops and establishments, and the other state registrations -----------
    ObligationSpec(
        code="labor.shops_establishment.annual",
        title="Shops and Establishments — Annual renewal and return",
        regulation=Regulation.LABOR,
        filing_type="S&E Renewal",
        frequency=Frequency.ANNUAL,
        section="State Shops and Commercial Establishments Act",
        description=(
            "Renewal of the establishment registration and the annual return, where "
            "the state requires one. Commonly due within three months of the year end."
        ),
        authority=_STATE_AUTHORITY,
        due_month=6,
        due_day=30,
        entity_types=EMPLOYERS,
        penalty_description="Fine per the state Act, commonly ₹5,000 to ₹25,000",
        penalty_max_paise=25_000 * RUPEE,
    ),
    ObligationSpec(
        code="labor.factories_annual_return.annual",
        title="Factories Act — Annual return (Form 21)",
        regulation=Regulation.LABOR,
        filing_type="Form 21",
        frequency=Frequency.ANNUAL,
        section="Rule 100, State Factories Rules under the Factories Act 1948",
        description=(
            "Annual return by an occupier of a registered factory, due 31 January for "
            "the preceding calendar year."
        ),
        authority=_STATE_AUTHORITY,
        due_month=1,
        due_day=31,
        entity_types=EMPLOYERS,
        industries=("manufacturing", "chemicals", "pharmaceuticals", "textiles", "engineering"),
        min_employees=10,
        penalty_description="Up to ₹1,00,000 or imprisonment under section 92",
        penalty_max_paise=100_000 * RUPEE,
    ),
    ObligationSpec(
        code="labor.contract_labour_return.annual",
        title="Contract Labour — Annual return (Form XXV)",
        regulation=Regulation.LABOR,
        filing_type="Form XXV",
        frequency=Frequency.ANNUAL,
        section="Rule 82(2), Contract Labour (Regulation and Abolition) Central Rules 1971",
        description=(
            "Annual return by a principal employer engaging 20 or more contract "
            "workers, due 15 February for the preceding calendar year."
        ),
        authority=_STATE_AUTHORITY,
        due_month=2,
        due_day=15,
        entity_types=EMPLOYERS,
        min_employees=20,
        penalty_description="Up to ₹1,000 and a daily fine for continuing contravention",
        penalty_per_day_paise=100 * RUPEE,
    ),
    ObligationSpec(
        code="labor.bonus_return.annual",
        title="Payment of Bonus — Annual return (Form D)",
        regulation=Regulation.LABOR,
        filing_type="Form D",
        frequency=Frequency.ANNUAL,
        section="Rule 5, Payment of Bonus Rules 1975",
        description=(
            "Return of bonus paid, filed within 30 days of the bonus becoming payable. "
            "Bonus itself is due within eight months of the year end, so the return "
            "follows at the end of December."
        ),
        authority=_STATE_AUTHORITY,
        due_month=12,
        due_day=31,
        entity_types=EMPLOYERS,
        min_employees=20,
        penalty_description="Up to ₹1,000 or six months' imprisonment under section 28",
        penalty_max_paise=1_000 * RUPEE,
    ),
    ObligationSpec(
        code="labor.gratuity_notice.event",
        title="Payment of Gratuity — Notice of opening and payment window",
        regulation=Regulation.LABOR,
        filing_type="Form A / Form L",
        frequency=Frequency.EVENT_BASED,
        section="Sections 7(3) and 7(3A), Payment of Gratuity Act 1972",
        description=(
            "Gratuity must be paid within 30 days of becoming payable, after which "
            "simple interest runs on the amount."
        ),
        authority=_STATE_AUTHORITY,
        offset_days=30,
        entity_types=EMPLOYERS,
        min_employees=10,
        penalty_description=(
            "Simple interest on the unpaid amount, plus prosecution under section 9"
        ),
    ),
    ObligationSpec(
        code="labor.maternity_return.annual",
        title="Maternity Benefit — Annual return (Form 11)",
        regulation=Regulation.LABOR,
        filing_type="MB Form 11",
        frequency=Frequency.ANNUAL,
        section="Rule 16, Maternity Benefit (Mines and Circus) Rules read with state rules",
        description="Annual return of maternity benefit paid, due 21 January.",
        authority=_STATE_AUTHORITY,
        due_month=1,
        due_day=21,
        entity_types=EMPLOYERS,
        min_employees=10,
        penalty_description="Up to ₹5,000 and imprisonment under section 21",
        penalty_max_paise=5_000 * RUPEE,
    ),
    ObligationSpec(
        code="labor.posh_annual_report.annual",
        title="POSH — Annual report of the Internal Committee",
        regulation=Regulation.LABOR,
        filing_type="POSH Annual Report",
        frequency=Frequency.ANNUAL,
        section="Section 21, Sexual Harassment of Women at Workplace (Prevention, "
        "Prohibition and Redressal) Act 2013",
        description=(
            "The Internal Committee's annual report of complaints received and "
            "disposed of, filed with the District Officer and disclosed in the board's "
            "report. Due 31 January for the preceding calendar year."
        ),
        authority=_STATE_AUTHORITY,
        due_month=1,
        due_day=31,
        entity_types=EMPLOYERS,
        min_employees=10,
        penalty_description="Up to ₹50,000, rising on repetition to cancellation of licence",
        penalty_max_paise=50_000 * RUPEE,
    ),
    ObligationSpec(
        code="labor.lwf.half_yearly",
        title="Labour Welfare Fund — Contribution",
        regulation=Regulation.LABOR,
        filing_type="LWF Challan",
        frequency=Frequency.HALF_YEARLY,
        section="State Labour Welfare Fund Act",
        description=(
            "Employer and employee contributions to the state labour welfare fund, "
            "where the state operates one. Commonly half-yearly."
        ),
        authority=_STATE_AUTHORITY,
        offset_days=15,
        entity_types=EMPLOYERS,
        states=(
            "Andhra Pradesh",
            "Chandigarh",
            "Chhattisgarh",
            "Delhi",
            "Goa",
            "Gujarat",
            "Haryana",
            "Karnataka",
            "Kerala",
            "Madhya Pradesh",
            "Maharashtra",
            "Odisha",
            "Punjab",
            "Tamil Nadu",
            "Telangana",
            "West Bengal",
        ),
        min_employees=5,
        penalty_description="Interest and penalty per the state Act",
    ),
    ObligationSpec(
        code="labor.minimum_wages_revision.half_yearly",
        title="Minimum wages — Review against the revised state notification",
        regulation=Regulation.LABOR,
        filing_type="Wage Review",
        frequency=Frequency.HALF_YEARLY,
        section="Section 3, Minimum Wages Act 1948",
        description=(
            "States revise the variable dearness allowance every six months, usually "
            "with effect from 1 April and 1 October. Not a filing, but underpaying "
            "against a revised notification is recoverable with a penalty, so it "
            "belongs on the calendar."
        ),
        authority=_STATE_AUTHORITY,
        offset_days=30,
        entity_types=EMPLOYERS,
        min_employees=1,
        penalty_description="Up to ₹10 times the shortfall, plus the arrears, under section 20",
        penalty_max_paise=1 * LAKH,
    ),
)
