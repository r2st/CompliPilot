"""Audit trail and dashboard bodies."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.models.enums import AuditAction, FilingStatus, Regulation
from app.schemas.common import ORMModel


class AuditEntryResponse(ORMModel):
    """One entry of the append-only chain.

    ``checksum`` and ``prev_checksum`` are returned rather than hidden as
    implementation detail. They are the evidence: a client that has kept an
    exported head checksum can check it against this response without trusting
    the verification endpoint, which is exactly the check that catches a
    compromised API answering "all fine".
    """

    id: int
    organization_id: int
    sequence: int
    user_id: int | None = None
    actor_label: str
    action: AuditAction
    entity_type: str
    entity_id: str | None = None
    timestamp: datetime
    ip_address: str | None = None
    request_id: str | None = None
    before_json: dict | None = None
    after_json: dict | None = None
    summary: str | None = None
    prev_checksum: str
    checksum: str


class ChainVerificationResponse(BaseModel):
    organization_id: int
    entries_checked: int
    is_valid: bool
    broken_at_sequence: int | None = None
    broken_entry_id: int | None = None
    reason: str | None = None
    head_checksum: str | None = None


class ChainHeadResponse(BaseModel):
    """The chain's current tip, for publishing outside the database."""

    organization_id: int
    sequence: int
    checksum: str
    entry_count: int
    as_of: str


class AuditExportRequest(BaseModel):
    """Bound an export. Both dates inclusive."""

    start_date: date | None = None
    end_date: date | None = None
    entity_type: str | None = Field(default=None, max_length=64)
    action: AuditAction | None = None


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


class ComplianceScore(BaseModel):
    """The headline number, and the parts it is made of.

    The components are returned alongside the score because a bare number
    changes without explanation and nobody trusts it twice. A CA who sees
    "78, down from 84" needs to know it moved because two filings went overdue.
    """

    score: int = Field(ge=0, le=100)
    band: str
    on_time_filings: int
    late_filings: int
    overdue_filings: int
    open_filings: int
    total_filings: int
    unacknowledged_impacts: int
    overdue_dpb_notifications: int


class RegulationBreakdown(BaseModel):
    regulation: Regulation
    total: int
    open: int
    overdue: int
    submitted: int


class DashboardResponse(BaseModel):
    """The SMB dashboard: one organization's compliance position."""

    organization_id: int
    organization_name: str
    as_of: date
    score: ComplianceScore
    upcoming: list[dict] = []
    overdue: list[dict] = []
    by_regulation: list[RegulationBreakdown] = []
    by_status: dict[str, int] = {}
    penalty_exposure_paise: int = 0
    documents_pending_parse: int = 0
    open_data_requests: int = 0
    open_breaches: int = 0


class ClientDashboardRow(BaseModel):
    """One client's line on a CA firm's consolidated view."""

    client_id: int
    organization_id: int
    name: str
    status: str
    assigned_user_id: int | None = None
    score: int
    band: str
    open_filings: int
    overdue_filings: int
    due_within_7_days: int
    penalty_exposure_paise: int
    next_due_date: date | None = None
    next_filing_type: str | None = None


class FirmDashboardResponse(BaseModel):
    """The CA firm's consolidated dashboard across every client it acts for."""

    ca_firm_id: int
    ca_firm_name: str
    as_of: date
    client_count: int
    clients: list[ClientDashboardRow] = []
    total_open_filings: int = 0
    total_overdue_filings: int = 0
    total_penalty_exposure_paise: int = 0
    average_score: int = 0
    # Clients with at least one overdue filing, worst first. The firm's actual
    # work queue, which is a different question from the alphabetical list.
    attention_required: list[ClientDashboardRow] = []


class WorkloadRow(BaseModel):
    """One staff member's load, for the firm's distribution view."""

    user_id: int
    full_name: str
    role: str
    client_count: int
    open_filings: int
    overdue_filings: int
    due_within_7_days: int


class FilingStatusCount(BaseModel):
    status: FilingStatus
    count: int
