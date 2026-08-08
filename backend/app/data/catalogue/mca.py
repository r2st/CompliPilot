"""MCA and ROC obligations under the Companies Act 2013 and the LLP Act 2008.

The MVP's second pillar. Two things about this family drive how it is modelled:

* **Most annual ROC dates hang off the AGM, not off the year end.** MGT-7 is due
  within 60 days of the annual general meeting and AOC-4 within 30. The AGM
  itself must be held by 30 September, so the design document's 31 October and
  30 November are the dates that follow from an AGM held on the last permitted
  day. Those are the dates used here, and they are the *latest* the filing can
  be due — a company that holds its AGM in August owes both earlier, which is
  why each row says so in its description rather than pretending the date is
  fixed.

* **The per-day penalty is the same ₹100 across almost every form**, and it is
  uncapped. That single fact is the strongest argument the product has for
  itself, so it is on every row where it applies.
"""
from __future__ import annotations

from app.data.catalogue.base import (
    COMPANIES,
    COMPANIES_AND_LLPS,
    CRORE,
    LAKH,
    RUPEE,
    ObligationSpec,
)
from app.models.enums import EntityType, Frequency, Regulation

_AUTHORITY = "Ministry of Corporate Affairs (MCA)"

# Section 403: additional fee for late filing of most forms. Uncapped, which is
# why a forgotten AOC-4 becomes a six-figure problem inside two years.
_ROC_PER_DAY = 100 * RUPEE
_ROC_PENALTY = "₹100 per day of delay, with no upper limit (section 403)"

MCA_OBLIGATIONS: tuple[ObligationSpec, ...] = (
    # --- Annual filings -----------------------------------------------------
    ObligationSpec(
        code="mca.mgt7.annual",
        title="MGT-7 — Annual return",
        regulation=Regulation.MCA,
        filing_type="MGT-7",
        frequency=Frequency.ANNUAL,
        section="Section 92, Companies Act 2013",
        description=(
            "Annual return covering shareholding, directors, meetings and "
            "indebtedness. Due within 60 days of the AGM; 31 October is the latest "
            "possible date, following an AGM held on 30 September."
        ),
        authority=_AUTHORITY,
        due_month=10,
        due_day=31,
        entity_types=(
            EntityType.PRIVATE_LIMITED,
            EntityType.PUBLIC_LIMITED,
            EntityType.SECTION_8,
        ),
        penalty_description=_ROC_PENALTY,
        penalty_per_day_paise=_ROC_PER_DAY,
    ),
    ObligationSpec(
        code="mca.mgt7a.annual",
        title="MGT-7A — Abridged annual return (OPC and small company)",
        regulation=Regulation.MCA,
        filing_type="MGT-7A",
        frequency=Frequency.ANNUAL,
        section="Section 92 read with Rule 11(1), Companies (Management and Administration) Rules",
        description=(
            "The shorter annual return available to a One Person Company and to a "
            "small company — paid-up capital up to ₹4 crore and turnover up to ₹40 crore."
        ),
        authority=_AUTHORITY,
        due_month=10,
        due_day=31,
        entity_types=(EntityType.ONE_PERSON_COMPANY, EntityType.PRIVATE_LIMITED),
        max_turnover_paise=40 * CRORE,
        penalty_description=_ROC_PENALTY,
        penalty_per_day_paise=_ROC_PER_DAY,
    ),
    ObligationSpec(
        code="mca.aoc4.annual",
        title="AOC-4 — Financial statements",
        regulation=Regulation.MCA,
        filing_type="AOC-4",
        frequency=Frequency.ANNUAL,
        section="Section 137, Companies Act 2013",
        description=(
            "Audited financial statements with the board's report and the auditor's "
            "report. Due within 30 days of the AGM; 30 November is the latest date."
        ),
        authority=_AUTHORITY,
        due_month=11,
        due_day=30,
        entity_types=COMPANIES,
        penalty_description=_ROC_PENALTY,
        penalty_per_day_paise=_ROC_PER_DAY,
    ),
    ObligationSpec(
        code="mca.aoc4_xbrl.annual",
        title="AOC-4 XBRL — Financial statements in XBRL",
        regulation=Regulation.MCA,
        filing_type="AOC-4 XBRL",
        frequency=Frequency.ANNUAL,
        section="Rule 3, Companies (Filing of Documents and Forms in XBRL) Rules 2015",
        description=(
            "XBRL-tagged financial statements, required of listed companies and of "
            "companies with paid-up capital of ₹5 crore or turnover of ₹100 crore."
        ),
        authority=_AUTHORITY,
        due_month=11,
        due_day=30,
        entity_types=COMPANIES,
        min_turnover_paise=100 * CRORE,
        penalty_description=_ROC_PENALTY,
        penalty_per_day_paise=_ROC_PER_DAY,
    ),
    ObligationSpec(
        code="mca.aoc4_cfs.annual",
        title="AOC-4 CFS — Consolidated financial statements",
        regulation=Regulation.MCA,
        filing_type="AOC-4 CFS",
        frequency=Frequency.ANNUAL,
        section="Section 129(3), Companies Act 2013",
        description="Consolidated statements, required of any company with a subsidiary.",
        authority=_AUTHORITY,
        due_month=11,
        due_day=30,
        entity_types=COMPANIES,
        penalty_description=_ROC_PENALTY,
        penalty_per_day_paise=_ROC_PER_DAY,
    ),
    ObligationSpec(
        code="mca.agm.annual",
        title="Annual General Meeting",
        regulation=Regulation.MCA,
        filing_type="AGM",
        frequency=Frequency.ANNUAL,
        section="Section 96, Companies Act 2013",
        description=(
            "Must be held within six months of the financial year end and no more "
            "than fifteen months after the previous one. Not a filing, but every "
            "annual ROC deadline is measured from it."
        ),
        authority=_AUTHORITY,
        due_month=9,
        due_day=30,
        entity_types=(
            EntityType.PRIVATE_LIMITED,
            EntityType.PUBLIC_LIMITED,
            EntityType.SECTION_8,
        ),
        penalty_description="Up to ₹1,00,000 on the company and ₹5,000 per day on officers",
        penalty_max_paise=100_000 * RUPEE,
    ),
    ObligationSpec(
        code="mca.dir3kyc.annual",
        title="DIR-3 KYC — Director KYC",
        regulation=Regulation.MCA,
        filing_type="DIR-3 KYC",
        frequency=Frequency.ANNUAL,
        section="Rule 12A, Companies (Appointment and Qualification of Directors) Rules 2014",
        description=(
            "Annual KYC for every person holding a DIN, due 30 September. A missed "
            "filing deactivates the DIN, which blocks every other form the director "
            "must sign."
        ),
        authority=_AUTHORITY,
        due_month=9,
        due_day=30,
        entity_types=COMPANIES_AND_LLPS,
        penalty_description="₹5,000 to reactivate a deactivated DIN",
        penalty_max_paise=5_000 * RUPEE,
    ),
    ObligationSpec(
        code="mca.dpt3.annual",
        title="DPT-3 — Return of deposits and exempted receipts",
        regulation=Regulation.MCA,
        filing_type="DPT-3",
        frequency=Frequency.ANNUAL,
        section="Rule 16, Companies (Acceptance of Deposits) Rules 2014",
        description=(
            "Annual return of money received that is not a deposit — including "
            "director loans and inter-corporate loans — due 30 June."
        ),
        authority=_AUTHORITY,
        due_month=6,
        due_day=30,
        entity_types=COMPANIES,
        penalty_description="Up to ₹1 crore on the company under section 76A, plus ₹100 per day",
        penalty_per_day_paise=_ROC_PER_DAY,
    ),
    ObligationSpec(
        code="mca.msme1.half_yearly",
        title="MSME-1 — Half-yearly return of dues to micro and small enterprises",
        regulation=Regulation.MCA,
        filing_type="MSME-1",
        frequency=Frequency.HALF_YEARLY,
        section="Section 405, Companies Act 2013 (Order dated 22 January 2019)",
        description=(
            "Return of payments to MSME suppliers outstanding beyond 45 days. Due "
            "31 October for April–September and 30 April for October–March."
        ),
        authority=_AUTHORITY,
        due_day=31,
        entity_types=COMPANIES,
        penalty_description="Up to ₹25,000 on the company and ₹25,000 on each officer",
        penalty_max_paise=25_000 * RUPEE,
    ),
    ObligationSpec(
        code="mca.bencf2.annual",
        title="BEN-2 — Return of significant beneficial owners",
        regulation=Regulation.MCA,
        filing_type="BEN-2",
        frequency=Frequency.EVENT_BASED,
        section="Section 90, Companies Act 2013",
        description=(
            "Filed within 30 days of receiving a BEN-1 declaration from a significant "
            "beneficial owner."
        ),
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=COMPANIES,
        penalty_description="₹10 lakh on the company, plus ₹1,000 per day of continuing default",
        penalty_per_day_paise=1_000 * RUPEE,
        penalty_max_paise=10 * LAKH,
    ),
    ObligationSpec(
        code="mca.cra4.annual",
        title="CRA-4 — Cost audit report",
        regulation=Regulation.MCA,
        filing_type="CRA-4",
        frequency=Frequency.ANNUAL,
        section="Rule 6(6), Companies (Cost Records and Audit) Rules 2014",
        description=(
            "Cost audit report, filed within 30 days of receipt from the cost auditor. "
            "Applies to notified regulated and non-regulated sectors above the "
            "turnover thresholds."
        ),
        authority=_AUTHORITY,
        due_month=9,
        due_day=30,
        entity_types=COMPANIES,
        min_turnover_paise=50 * CRORE,
        industries=("manufacturing", "pharmaceuticals", "chemicals", "power", "telecom"),
        penalty_description=_ROC_PENALTY,
        penalty_per_day_paise=_ROC_PER_DAY,
    ),
    # --- Event-based company filings ----------------------------------------
    ObligationSpec(
        code="mca.inc20a.one_time",
        title="INC-20A — Declaration of commencement of business",
        regulation=Regulation.MCA,
        filing_type="INC-20A",
        frequency=Frequency.ONE_TIME,
        section="Section 10A, Companies Act 2013",
        description=(
            "Filed within 180 days of incorporation, declaring that the subscribers "
            "have paid for their shares. Until it is filed the company may not borrow "
            "or commence business."
        ),
        authority=_AUTHORITY,
        offset_days=180,
        entity_types=COMPANIES,
        penalty_description=(
            "₹50,000 on the company and ₹1,000 per day on each officer, up to ₹1,00,000; "
            "the Registrar may strike the company off"
        ),
        penalty_per_day_paise=1_000 * RUPEE,
        penalty_max_paise=100_000 * RUPEE,
    ),
    ObligationSpec(
        code="mca.adt1.event",
        title="ADT-1 — Notice of auditor appointment",
        regulation=Regulation.MCA,
        filing_type="ADT-1",
        frequency=Frequency.EVENT_BASED,
        section="Section 139, Companies Act 2013",
        description="Filed within 15 days of the AGM at which the auditor was appointed.",
        authority=_AUTHORITY,
        offset_days=15,
        entity_types=COMPANIES,
        penalty_description=_ROC_PENALTY,
        penalty_per_day_paise=_ROC_PER_DAY,
    ),
    ObligationSpec(
        code="mca.adt3.event",
        title="ADT-3 — Notice of auditor resignation",
        regulation=Regulation.MCA,
        filing_type="ADT-3",
        frequency=Frequency.EVENT_BASED,
        section="Section 140(2), Companies Act 2013",
        description="Filed by the resigning auditor within 30 days of resignation.",
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=COMPANIES,
        penalty_description="₹50,000 or the auditor's remuneration, whichever is less",
        penalty_max_paise=50_000 * RUPEE,
    ),
    ObligationSpec(
        code="mca.dir12.event",
        title="DIR-12 — Change in directors or key managerial personnel",
        regulation=Regulation.MCA,
        filing_type="DIR-12",
        frequency=Frequency.EVENT_BASED,
        section="Sections 7(1)(c), 168 and 170(2), Companies Act 2013",
        description="Filed within 30 days of an appointment, resignation or change in KMP.",
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=COMPANIES,
        penalty_description=_ROC_PENALTY,
        penalty_per_day_paise=_ROC_PER_DAY,
    ),
    ObligationSpec(
        code="mca.pas3.event",
        title="PAS-3 — Return of allotment of shares",
        regulation=Regulation.MCA,
        filing_type="PAS-3",
        frequency=Frequency.EVENT_BASED,
        section="Section 39(4), Companies Act 2013",
        description="Filed within 30 days of an allotment of securities.",
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=COMPANIES,
        penalty_description="₹1,000 per day up to ₹25 lakh on the company and on each officer",
        penalty_per_day_paise=1_000 * RUPEE,
        penalty_max_paise=25 * LAKH,
    ),
    ObligationSpec(
        code="mca.mgt14.event",
        title="MGT-14 — Filing of board and shareholder resolutions",
        regulation=Regulation.MCA,
        filing_type="MGT-14",
        frequency=Frequency.EVENT_BASED,
        section="Section 117, Companies Act 2013",
        description="Filed within 30 days of passing a resolution to which section 117 applies.",
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=COMPANIES,
        penalty_description="₹10,000 plus ₹100 per day, up to ₹2,00,000 on the company",
        penalty_per_day_paise=_ROC_PER_DAY,
        penalty_max_paise=200_000 * RUPEE,
    ),
    ObligationSpec(
        code="mca.chg1.event",
        title="CHG-1 — Registration of a charge",
        regulation=Regulation.MCA,
        filing_type="CHG-1",
        frequency=Frequency.EVENT_BASED,
        section="Section 77, Companies Act 2013",
        description=(
            "Filed within 30 days of creating or modifying a charge over the company's "
            "property. An unregistered charge is void against a liquidator."
        ),
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=COMPANIES_AND_LLPS,
        penalty_description="Ad valorem additional fees; the charge is void if not registered",
    ),
    ObligationSpec(
        code="mca.chg4.event",
        title="CHG-4 — Satisfaction of a charge",
        regulation=Regulation.MCA,
        filing_type="CHG-4",
        frequency=Frequency.EVENT_BASED,
        section="Section 82, Companies Act 2013",
        description="Filed within 30 days of a charge being fully satisfied.",
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=COMPANIES_AND_LLPS,
        penalty_description=_ROC_PENALTY,
        penalty_per_day_paise=_ROC_PER_DAY,
    ),
    ObligationSpec(
        code="mca.inc22.event",
        title="INC-22 — Change of registered office",
        regulation=Regulation.MCA,
        filing_type="INC-22",
        frequency=Frequency.EVENT_BASED,
        section="Section 12(4), Companies Act 2013",
        description="Filed within 30 days of a change in the registered office address.",
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=COMPANIES,
        penalty_description="₹1,000 per day, up to ₹1,00,000",
        penalty_per_day_paise=1_000 * RUPEE,
        penalty_max_paise=100_000 * RUPEE,
    ),
    # --- LLP ----------------------------------------------------------------
    ObligationSpec(
        code="mca.llp_form11.annual",
        title="LLP Form 11 — Annual return",
        regulation=Regulation.MCA,
        filing_type="Form 11",
        frequency=Frequency.ANNUAL,
        section="Section 35, Limited Liability Partnership Act 2008",
        description="Annual return of an LLP, due 30 May — two months after the year end.",
        authority=_AUTHORITY,
        due_month=5,
        due_day=30,
        entity_types=(EntityType.LLP,),
        penalty_description="₹100 per day of delay, with no upper limit",
        penalty_per_day_paise=_ROC_PER_DAY,
    ),
    ObligationSpec(
        code="mca.llp_form8.annual",
        title="LLP Form 8 — Statement of account and solvency",
        regulation=Regulation.MCA,
        filing_type="Form 8",
        frequency=Frequency.ANNUAL,
        section="Section 34(2), Limited Liability Partnership Act 2008",
        description="Statement of account and solvency, due 30 October.",
        authority=_AUTHORITY,
        due_month=10,
        due_day=30,
        entity_types=(EntityType.LLP,),
        penalty_description="₹100 per day of delay, with no upper limit",
        penalty_per_day_paise=_ROC_PER_DAY,
    ),
    ObligationSpec(
        code="mca.llp_form3.event",
        title="LLP Form 3 — Change in the LLP agreement",
        regulation=Regulation.MCA,
        filing_type="Form 3",
        frequency=Frequency.EVENT_BASED,
        section="Section 23(2), Limited Liability Partnership Act 2008",
        description="Filed within 30 days of a change to the LLP agreement.",
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=(EntityType.LLP,),
        penalty_description="₹100 per day of delay",
        penalty_per_day_paise=_ROC_PER_DAY,
    ),
    ObligationSpec(
        code="mca.llp_form4.event",
        title="LLP Form 4 — Change in partners or designated partners",
        regulation=Regulation.MCA,
        filing_type="Form 4",
        frequency=Frequency.EVENT_BASED,
        section="Section 25(2), Limited Liability Partnership Act 2008",
        description="Filed within 30 days of a partner joining or leaving.",
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=(EntityType.LLP,),
        penalty_description="₹100 per day of delay",
        penalty_per_day_paise=_ROC_PER_DAY,
    ),
)
