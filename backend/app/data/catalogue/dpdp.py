"""DPDP Act obligations.

The newest family and the one with the fewest fixed dates, because the Act is
built around continuous duties rather than periodic returns. What it does have
is a very short clock on the one event that matters: a personal data breach must
be reported to the Data Protection Board without delay, and the product treats
72 hours as the operative window (see
:data:`app.models.dpdp.BREACH_NOTIFICATION_HOURS`).

Every row carries ``requires_personal_data=True``. In practice that flag is true
of essentially every organization — a company with employees is processing
personal data — which is why the organization profile defaults it to true rather
than false. The flag exists so the handful of entities that genuinely process
none can switch the family off, not as a filter that will usually exclude.

The penalties here are an order of magnitude larger than anything else in the
catalogue. Schedule 1 of the Act runs to ₹250 crore for a failure to take
reasonable security safeguards. That number is on the row because it is the
single most persuasive line the product can put in front of a founder.
"""
from __future__ import annotations

from app.data.catalogue.base import CRORE, GST_REGISTRABLE, ObligationSpec
from app.models.enums import Frequency, Regulation

_AUTHORITY = "Data Protection Board of India"

DPDP_OBLIGATIONS: tuple[ObligationSpec, ...] = (
    ObligationSpec(
        code="dpdp.breach_notification.event",
        title="Breach notification to the Data Protection Board",
        regulation=Regulation.DPDP,
        filing_type="Breach Notification",
        frequency=Frequency.EVENT_BASED,
        section="Section 8(6), Digital Personal Data Protection Act 2023",
        description=(
            "A data fiduciary must inform the Board and every affected data principal "
            "of a personal data breach. The product runs a 72-hour clock from "
            "detection — the fiduciary cannot report what it does not yet know about, "
            "so the clock starts at discovery rather than at the intrusion."
        ),
        authority=_AUTHORITY,
        offset_days=3,
        entity_types=GST_REGISTRABLE,
        requires_personal_data=True,
        penalty_description="Up to ₹200 crore for failure to notify a breach (Schedule 1)",
        penalty_max_paise=200 * CRORE,
    ),
    ObligationSpec(
        code="dpdp.principal_notification.event",
        title="Breach notification to affected data principals",
        regulation=Regulation.DPDP,
        filing_type="Principal Notification",
        frequency=Frequency.EVENT_BASED,
        section="Section 8(6), Digital Personal Data Protection Act 2023",
        description=(
            "Each affected individual must be told, in clear language, what happened, "
            "what data was involved and what they can do about it. Tracked separately "
            "from the Board notification because the two are distinct duties and a "
            "fiduciary can discharge one and forget the other."
        ),
        authority=_AUTHORITY,
        offset_days=3,
        entity_types=GST_REGISTRABLE,
        requires_personal_data=True,
        penalty_description="Up to ₹200 crore for failure to notify affected principals",
        penalty_max_paise=200 * CRORE,
    ),
    ObligationSpec(
        code="dpdp.dsr_response.event",
        title="Response to a data principal request",
        regulation=Regulation.DPDP,
        filing_type="DSR Response",
        frequency=Frequency.EVENT_BASED,
        section="Sections 11 to 14, Digital Personal Data Protection Act 2023",
        description=(
            "A request for access, correction, erasure or grievance redressal must be "
            "answered within the period the Rules prescribe. Thirty days is used until "
            "a number is notified; see DSR_SLA_DAYS."
        ),
        authority=_AUTHORITY,
        offset_days=30,
        entity_types=GST_REGISTRABLE,
        requires_personal_data=True,
        penalty_description="Up to ₹50 crore for breach of the fiduciary's other duties",
        penalty_max_paise=50 * CRORE,
    ),
    ObligationSpec(
        code="dpdp.consent_review.annual",
        title="Annual review of consent notices and records",
        regulation=Regulation.DPDP,
        filing_type="Consent Review",
        frequency=Frequency.ANNUAL,
        section="Sections 5 to 7, Digital Personal Data Protection Act 2023",
        description=(
            "An annual review that every purpose being processed is covered by a valid, "
            "current consent notice in the languages the Act requires, and that "
            "withdrawn consents have propagated to the systems named in the data map."
        ),
        authority=_AUTHORITY,
        due_month=3,
        due_day=31,
        period_offset=0,
        entity_types=GST_REGISTRABLE,
        requires_personal_data=True,
        penalty_description="Up to ₹50 crore for processing without a valid consent",
        penalty_max_paise=50 * CRORE,
    ),
    ObligationSpec(
        code="dpdp.data_map_review.annual",
        title="Annual review of the personal data map",
        regulation=Regulation.DPDP,
        filing_type="Data Map Review",
        frequency=Frequency.ANNUAL,
        section="Section 8, Digital Personal Data Protection Act 2023",
        description=(
            "The record of what personal data is held, where, why and for how long. "
            "Reviewed annually — a stale map makes both a breach assessment and an "
            "erasure request unanswerable."
        ),
        authority=_AUTHORITY,
        due_month=3,
        due_day=31,
        period_offset=0,
        entity_types=GST_REGISTRABLE,
        requires_personal_data=True,
        penalty_description="Up to ₹250 crore for failure to take reasonable security safeguards",
        penalty_max_paise=250 * CRORE,
    ),
    ObligationSpec(
        code="dpdp.retention_purge.annual",
        title="Erasure of personal data past its retention period",
        regulation=Regulation.DPDP,
        filing_type="Retention Purge",
        frequency=Frequency.ANNUAL,
        section="Section 8(7), Digital Personal Data Protection Act 2023",
        description=(
            "Personal data must be erased once the purpose is no longer being served "
            "and no law requires it be kept. An annual purge against the retention "
            "periods recorded in the data map."
        ),
        authority=_AUTHORITY,
        due_month=3,
        due_day=31,
        period_offset=0,
        entity_types=GST_REGISTRABLE,
        requires_personal_data=True,
        penalty_description="Up to ₹50 crore for breach of the fiduciary's duties",
        penalty_max_paise=50 * CRORE,
    ),
    ObligationSpec(
        code="dpdp.dpia.annual",
        title="Data Protection Impact Assessment — Significant Data Fiduciary",
        regulation=Regulation.DPDP,
        filing_type="DPIA",
        frequency=Frequency.ANNUAL,
        section="Section 10(2)(c), Digital Personal Data Protection Act 2023",
        description=(
            "A Significant Data Fiduciary must carry out a periodic impact assessment "
            "and a periodic audit. Only entities notified as significant owe this; the "
            "row is scoped by turnover as a proxy until the notification criteria are "
            "published."
        ),
        authority=_AUTHORITY,
        due_month=3,
        due_day=31,
        period_offset=0,
        entity_types=GST_REGISTRABLE,
        requires_personal_data=True,
        min_turnover_paise=250 * CRORE,
        penalty_description="Up to ₹150 crore for a Significant Data Fiduciary's breach of duties",
        penalty_max_paise=150 * CRORE,
    ),
    ObligationSpec(
        code="dpdp.dpo_appointment.one_time",
        title="Appointment of a Data Protection Officer",
        regulation=Regulation.DPDP,
        filing_type="DPO Appointment",
        frequency=Frequency.ONE_TIME,
        section="Section 10(2)(a), Digital Personal Data Protection Act 2023",
        description=(
            "A Significant Data Fiduciary must appoint a Data Protection Officer based "
            "in India who reports to the board. Due within 90 days of being notified as "
            "significant."
        ),
        authority=_AUTHORITY,
        offset_days=90,
        entity_types=GST_REGISTRABLE,
        requires_personal_data=True,
        min_turnover_paise=250 * CRORE,
        penalty_description="Up to ₹150 crore for a Significant Data Fiduciary's breach of duties",
        penalty_max_paise=150 * CRORE,
    ),
    ObligationSpec(
        code="dpdp.independent_audit.annual",
        title="Independent data audit — Significant Data Fiduciary",
        regulation=Regulation.DPDP,
        filing_type="Data Audit",
        frequency=Frequency.ANNUAL,
        section="Section 10(2)(c), Digital Personal Data Protection Act 2023",
        description=(
            "An annual audit by an independent data auditor, evidencing compliance with "
            "the Act."
        ),
        authority=_AUTHORITY,
        due_month=6,
        due_day=30,
        entity_types=GST_REGISTRABLE,
        requires_personal_data=True,
        min_turnover_paise=250 * CRORE,
        penalty_description="Up to ₹150 crore for a Significant Data Fiduciary's breach of duties",
        penalty_max_paise=150 * CRORE,
    ),
)
