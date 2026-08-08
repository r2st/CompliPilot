"""GST obligations.

The highest-volume family in the product — an SMB with one GSTIN files at least
24 returns a year — and the one the MVP is judged on.

**A note on the turnover thresholds.** The ₹5 crore line separates QRMP-eligible
taxpayers from those who must file monthly, and the ₹5 crore line also triggers
GSTR-9C. They are the same number for different reasons and are written out
separately below rather than shared through a constant, because they move
independently: the QRMP threshold has changed twice since 2020 and the audit
threshold has not.
"""
from __future__ import annotations

from app.data.catalogue.base import CRORE, GST_REGISTRABLE, RUPEE, ObligationSpec
from app.models.enums import EntityType, Frequency, Regulation

_AUTHORITY = "Central Board of Indirect Taxes and Customs (CBIC)"

# The late fee that applies to most GST returns: ₹50 per day (₹25 CGST + ₹25
# SGST), or ₹20 for a nil return, capped. Written once because it is the same
# figure on a dozen rows and a divergence between them would be a bug.
_LATE_FEE_PER_DAY = 50 * RUPEE
_LATE_FEE_CAP = 5_000 * RUPEE

GST_OBLIGATIONS: tuple[ObligationSpec, ...] = (
    # --- Core monthly returns --------------------------------------------
    ObligationSpec(
        code="gst.gstr1.monthly",
        title="GSTR-1 — Outward supplies (monthly)",
        regulation=Regulation.GST,
        filing_type="GSTR-1",
        frequency=Frequency.MONTHLY,
        section="Section 37, CGST Act 2017 read with Rule 59",
        description=(
            "Statement of outward supplies for the month. Due the 11th of the "
            "following month for taxpayers not under QRMP."
        ),
        authority=_AUTHORITY,
        due_day=11,
        entity_types=GST_REGISTRABLE,
        min_turnover_paise=5 * CRORE,
        penalty_description="₹50 per day of delay (₹20 for a nil return), capped at ₹5,000",
        penalty_per_day_paise=_LATE_FEE_PER_DAY,
        penalty_max_paise=_LATE_FEE_CAP,
    ),
    ObligationSpec(
        code="gst.gstr3b.monthly",
        title="GSTR-3B — Summary return and tax payment (monthly)",
        regulation=Regulation.GST,
        filing_type="GSTR-3B",
        frequency=Frequency.MONTHLY,
        section="Section 39, CGST Act 2017 read with Rule 61",
        description=(
            "Summary of outward and inward supplies with the tax payable. This is "
            "the return that actually discharges the liability, so a late GSTR-3B "
            "carries interest at 18% per annum on top of the late fee."
        ),
        authority=_AUTHORITY,
        due_day=20,
        entity_types=GST_REGISTRABLE,
        min_turnover_paise=5 * CRORE,
        penalty_description=(
            "₹50 per day (₹20 nil), capped at ₹5,000, plus interest at 18% per annum "
            "on the unpaid tax"
        ),
        penalty_per_day_paise=_LATE_FEE_PER_DAY,
        penalty_max_paise=_LATE_FEE_CAP,
    ),
    # --- QRMP: the same two returns on a quarterly cadence ----------------
    # Separate codes rather than a frequency override on the monthly rows,
    # because a taxpayer moving into QRMP mid-year has genuinely different
    # obligations from that quarter on, and the calendar must show the
    # historical monthly filings alongside the new quarterly ones.
    ObligationSpec(
        code="gst.gstr1.quarterly",
        title="GSTR-1 — Outward supplies (QRMP, quarterly)",
        regulation=Regulation.GST,
        filing_type="GSTR-1",
        frequency=Frequency.QUARTERLY,
        section="Section 37, CGST Act 2017 read with Rule 59(1)",
        description=(
            "Quarterly statement of outward supplies for taxpayers under the "
            "Quarterly Return Monthly Payment scheme. Due the 13th of the month "
            "following the quarter."
        ),
        authority=_AUTHORITY,
        due_day=13,
        entity_types=GST_REGISTRABLE,
        max_turnover_paise=5 * CRORE,
        penalty_description="₹50 per day of delay (₹20 for a nil return), capped at ₹5,000",
        penalty_per_day_paise=_LATE_FEE_PER_DAY,
        penalty_max_paise=_LATE_FEE_CAP,
    ),
    ObligationSpec(
        code="gst.gstr3b.quarterly",
        title="GSTR-3B — Summary return (QRMP, quarterly)",
        regulation=Regulation.GST,
        filing_type="GSTR-3B",
        frequency=Frequency.QUARTERLY,
        section="Section 39(1) proviso, CGST Act 2017",
        description=(
            "Quarterly summary return under QRMP. Note that tax is still paid "
            "monthly through PMT-06 — the return is quarterly, the money is not."
        ),
        authority=_AUTHORITY,
        due_day=22,
        entity_types=GST_REGISTRABLE,
        max_turnover_paise=5 * CRORE,
        penalty_description=(
            "₹50 per day (₹20 nil), capped at ₹5,000, plus interest at 18% per annum"
        ),
        penalty_per_day_paise=_LATE_FEE_PER_DAY,
        penalty_max_paise=_LATE_FEE_CAP,
        metadata={
            "note": (
                "The statutory due date is the 22nd or the 24th depending on the "
                "state group. The earlier date is used so the reminder is never late."
            )
        },
    ),
    ObligationSpec(
        code="gst.pmt06.monthly",
        title="PMT-06 — Monthly tax payment under QRMP",
        regulation=Regulation.GST,
        filing_type="PMT-06",
        frequency=Frequency.MONTHLY,
        section="Rule 61A, CGST Rules 2017",
        description=(
            "Monthly tax deposit for the first two months of a QRMP quarter. Not a "
            "return, but a missed deposit accrues the same 18% interest."
        ),
        authority=_AUTHORITY,
        due_day=25,
        entity_types=GST_REGISTRABLE,
        max_turnover_paise=5 * CRORE,
        penalty_description="Interest at 18% per annum on the shortfall",
    ),
    # --- Annual ------------------------------------------------------------
    ObligationSpec(
        code="gst.gstr9.annual",
        title="GSTR-9 — Annual return",
        regulation=Regulation.GST,
        filing_type="GSTR-9",
        frequency=Frequency.ANNUAL,
        section="Section 44, CGST Act 2017 read with Rule 80",
        description=(
            "Consolidated annual return reconciling the year's GSTR-1 and GSTR-3B "
            "filings. Optional below ₹2 crore turnover, mandatory above."
        ),
        authority=_AUTHORITY,
        due_month=12,
        due_day=31,
        entity_types=GST_REGISTRABLE,
        min_turnover_paise=2 * CRORE,
        penalty_description="₹200 per day (₹100 CGST + ₹100 SGST), capped at 0.5% of turnover",
        penalty_per_day_paise=200 * RUPEE,
    ),
    ObligationSpec(
        code="gst.gstr9c.annual",
        title="GSTR-9C — Reconciliation statement",
        regulation=Regulation.GST,
        filing_type="GSTR-9C",
        frequency=Frequency.ANNUAL,
        section="Section 44(2), CGST Act 2017 read with Rule 80(3)",
        description=(
            "Self-certified reconciliation between the audited financial statements "
            "and the annual return. Required above ₹5 crore aggregate turnover."
        ),
        authority=_AUTHORITY,
        due_month=12,
        due_day=31,
        entity_types=GST_REGISTRABLE,
        min_turnover_paise=5 * CRORE,
        penalty_description="General penalty up to ₹25,000 under section 125",
        penalty_max_paise=25_000 * RUPEE,
    ),
    # --- Composition scheme -------------------------------------------------
    ObligationSpec(
        code="gst.cmp08.quarterly",
        title="CMP-08 — Composition levy statement",
        regulation=Regulation.GST,
        filing_type="CMP-08",
        frequency=Frequency.QUARTERLY,
        section="Rule 62, CGST Rules 2017",
        description="Quarterly statement and payment for composition taxpayers.",
        authority=_AUTHORITY,
        due_day=18,
        entity_types=GST_REGISTRABLE,
        max_turnover_paise=int(1.5 * CRORE),
        penalty_description="₹50 per day of delay, capped at ₹5,000",
        penalty_per_day_paise=_LATE_FEE_PER_DAY,
        penalty_max_paise=_LATE_FEE_CAP,
    ),
    ObligationSpec(
        code="gst.gstr4.annual",
        title="GSTR-4 — Annual return for composition taxpayers",
        regulation=Regulation.GST,
        filing_type="GSTR-4",
        frequency=Frequency.ANNUAL,
        section="Section 39(2), CGST Act 2017 read with Rule 62",
        description="Annual return for taxpayers who opted into the composition levy.",
        authority=_AUTHORITY,
        due_month=6,
        due_day=30,
        entity_types=GST_REGISTRABLE,
        max_turnover_paise=int(1.5 * CRORE),
        penalty_description="₹50 per day (₹20 nil), capped at ₹2,000",
        penalty_per_day_paise=_LATE_FEE_PER_DAY,
        penalty_max_paise=2_000 * RUPEE,
    ),
    # --- Special registrations ---------------------------------------------
    ObligationSpec(
        code="gst.gstr5.monthly",
        title="GSTR-5 — Return for non-resident taxable persons",
        regulation=Regulation.GST,
        filing_type="GSTR-5",
        frequency=Frequency.MONTHLY,
        section="Section 39(5), CGST Act 2017",
        description="Monthly return for a non-resident holding a temporary registration.",
        authority=_AUTHORITY,
        due_day=13,
        entity_types=(EntityType.FOREIGN_COMPANY,),
        penalty_description="₹50 per day of delay, capped at ₹5,000",
        penalty_per_day_paise=_LATE_FEE_PER_DAY,
        penalty_max_paise=_LATE_FEE_CAP,
    ),
    ObligationSpec(
        code="gst.gstr6.monthly",
        title="GSTR-6 — Input Service Distributor return",
        regulation=Regulation.GST,
        filing_type="GSTR-6",
        frequency=Frequency.MONTHLY,
        section="Section 39(4), CGST Act 2017",
        description="Monthly return distributing input tax credit to branch registrations.",
        authority=_AUTHORITY,
        due_day=13,
        entity_types=GST_REGISTRABLE,
        penalty_description="₹50 per day of delay",
        penalty_per_day_paise=_LATE_FEE_PER_DAY,
    ),
    ObligationSpec(
        code="gst.gstr7.monthly",
        title="GSTR-7 — TDS return under GST",
        regulation=Regulation.GST,
        filing_type="GSTR-7",
        frequency=Frequency.MONTHLY,
        section="Section 51, CGST Act 2017",
        description=(
            "Monthly return for entities required to deduct tax at source under GST — "
            "government bodies and notified public sector undertakings."
        ),
        authority=_AUTHORITY,
        due_day=10,
        entity_types=GST_REGISTRABLE,
        penalty_description="₹50 per day of delay, capped at ₹2,000",
        penalty_per_day_paise=_LATE_FEE_PER_DAY,
        penalty_max_paise=2_000 * RUPEE,
    ),
    ObligationSpec(
        code="gst.gstr8.monthly",
        title="GSTR-8 — TCS return for e-commerce operators",
        regulation=Regulation.GST,
        filing_type="GSTR-8",
        frequency=Frequency.MONTHLY,
        section="Section 52, CGST Act 2017",
        description="Monthly statement of supplies made through the platform and tax collected.",
        authority=_AUTHORITY,
        due_day=10,
        entity_types=GST_REGISTRABLE,
        industries=("e-commerce", "marketplace"),
        penalty_description="₹200 per day of delay, capped at ₹5,000",
        penalty_per_day_paise=200 * RUPEE,
        penalty_max_paise=_LATE_FEE_CAP,
    ),
    ObligationSpec(
        code="gst.itc04.half_yearly",
        title="ITC-04 — Goods sent to a job worker",
        regulation=Regulation.GST,
        filing_type="ITC-04",
        frequency=Frequency.HALF_YEARLY,
        section="Rule 45(3), CGST Rules 2017",
        description=(
            "Statement of inputs and capital goods sent to and received back from a "
            "job worker. Half-yearly above ₹5 crore turnover, annual below."
        ),
        authority=_AUTHORITY,
        due_day=25,
        entity_types=GST_REGISTRABLE,
        min_turnover_paise=5 * CRORE,
        industries=("manufacturing", "textiles", "engineering"),
        penalty_description="General penalty up to ₹25,000 under section 125",
        penalty_max_paise=25_000 * RUPEE,
    ),
    # --- Event-based ---------------------------------------------------------
    ObligationSpec(
        code="gst.gstr10.final",
        title="GSTR-10 — Final return on cancellation of registration",
        regulation=Regulation.GST,
        filing_type="GSTR-10",
        frequency=Frequency.EVENT_BASED,
        section="Section 45, CGST Act 2017",
        description=(
            "Filed within three months of a registration being cancelled or "
            "surrendered. Missing it blocks a fresh registration on the same PAN."
        ),
        authority=_AUTHORITY,
        offset_days=90,
        entity_types=GST_REGISTRABLE,
        penalty_description="₹100 per day, capped at ₹10,000",
        penalty_per_day_paise=100 * RUPEE,
        penalty_max_paise=10_000 * RUPEE,
    ),
    ObligationSpec(
        code="gst.lut.annual",
        title="LUT — Letter of Undertaking for zero-rated exports",
        regulation=Regulation.GST,
        filing_type="RFD-11",
        frequency=Frequency.ANNUAL,
        section="Rule 96A, CGST Rules 2017",
        description=(
            "Annual undertaking allowing export without payment of IGST. Must be in "
            "place before the financial year's first export, so it is dated to the "
            "start of the year rather than its end."
        ),
        authority=_AUTHORITY,
        due_month=4,
        due_day=30,
        period_offset=0,
        entity_types=GST_REGISTRABLE,
        industries=("export", "software-export", "manufacturing"),
        penalty_description=(
            "No direct penalty; without a valid LUT, exports must be made on payment "
            "of IGST and the tax recovered as a refund"
        ),
    ),
    ObligationSpec(
        code="gst.eway.event",
        title="E-way bill — Consignment above ₹50,000",
        regulation=Regulation.GST,
        filing_type="EWB-01",
        frequency=Frequency.EVENT_BASED,
        section="Rule 138, CGST Rules 2017",
        description=(
            "Generated before movement of goods worth more than ₹50,000. Recorded as "
            "an obligation so that a consignment logged in the system produces a "
            "checkable deadline; day-to-day generation is handled by GSTBot."
        ),
        authority=_AUTHORITY,
        offset_days=0,
        entity_types=GST_REGISTRABLE,
        penalty_description=(
            "₹10,000 or the tax sought to be evaded, whichever is higher, plus "
            "detention of the consignment"
        ),
        penalty_max_paise=10_000 * RUPEE,
    ),
)
