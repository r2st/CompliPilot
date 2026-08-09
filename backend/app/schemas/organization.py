"""Organization profile and CA-firm engagement bodies.

The statutory identifiers are the interesting part. They are stored encrypted
with a keyed fingerprint beside them (see :mod:`app.core.crypto`), so the
response schemas here never expose the ciphertext column directly — the router
decrypts into these fields, and :class:`OrganizationSummary`, which is what a
list endpoint returns, omits them entirely. A CA firm's client list does not
need 200 PANs decrypted to render.
"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import EngagementStatus, EngagementType, EntityType, OrgType, PlanTier
from app.schemas.common import (
    IdentifierMixin,
    ORMModel,
    Paise,
    validate_phone,
)


class OrganizationSummary(ORMModel):
    """An organization as it appears in a list. No decrypted identifiers."""

    id: int
    name: str
    type: OrgType
    entity_type: EntityType | None = None
    state: str | None = None
    industry: str | None = None
    is_active: bool
    plan_tier: PlanTier
    created_at: datetime


class OrganizationResponse(OrganizationSummary):
    """The full profile, including the identifiers, decrypted by the router."""

    legal_name: str | None = None
    gstin: str | None = None
    pan: str | None = None
    cin: str | None = None
    llpin: str | None = None
    tan: str | None = None
    firm_registration_no: str | None = None

    annual_turnover_paise: int | None = None
    employee_count: int | None = None
    incorporation_date: date | None = None
    financial_year_end_month: int = 3

    is_listed: bool = False
    has_foreign_investment: bool = False
    handles_personal_data: bool = True

    contact_email: str | None = None
    contact_phone: str | None = None
    address: str | None = None
    metadata_json: dict | None = None
    updated_at: datetime


class OrganizationProfileUpdate(IdentifierMixin):
    """A profile edit. Every field optional — this is a PATCH.

    ``None`` therefore has to mean "leave alone" rather than "clear", which is
    why there is no way to unset an identifier through this schema. Clearing a
    GSTIN is rare, destructive (it frees the uniqueness slot) and worth its own
    deliberate endpoint rather than being reachable by omitting a key.
    """

    name: str | None = Field(default=None, min_length=2, max_length=255)
    legal_name: str | None = Field(default=None, max_length=255)
    entity_type: EntityType | None = None
    state: str | None = Field(default=None, max_length=64)
    industry: str | None = Field(default=None, max_length=128)

    annual_turnover_paise: Paise | None = None
    employee_count: int | None = Field(default=None, ge=0, le=10_000_000)
    incorporation_date: date | None = None
    financial_year_end_month: int | None = Field(default=None, ge=1, le=12)

    is_listed: bool | None = None
    has_foreign_investment: bool | None = None
    handles_personal_data: bool | None = None

    contact_email: EmailStr | None = None
    contact_phone: str | None = None
    address: str | None = Field(default=None, max_length=2000)
    firm_registration_no: str | None = Field(default=None, max_length=32)
    metadata_json: dict | None = None

    _v_contact_phone = field_validator("contact_phone")(validate_phone)


class OrganizationCreateRequest(IdentifierMixin):
    """Onboard a client organization from inside a CA firm.

    Distinct from registration: no user is created, no password is set, and the
    caller is an authenticated CA firm rather than an anonymous signup. The
    client company gets an organization row so that its filings, documents and
    audit trail are tenant-scoped to *it* — which is what lets the engagement be
    handed to another firm later without moving any data.
    """

    name: str = Field(min_length=2, max_length=255)
    legal_name: str | None = Field(default=None, max_length=255)
    entity_type: EntityType | None = None
    state: str | None = Field(default=None, max_length=64)
    industry: str | None = Field(default=None, max_length=128)

    annual_turnover_paise: Paise | None = None
    employee_count: int | None = Field(default=None, ge=0, le=10_000_000)
    incorporation_date: date | None = None

    is_listed: bool = False
    has_foreign_investment: bool = False
    handles_personal_data: bool = True

    contact_email: EmailStr | None = None
    contact_phone: str | None = None
    address: str | None = Field(default=None, max_length=2000)

    _v_contact_phone = field_validator("contact_phone")(validate_phone)


# --------------------------------------------------------------------------
# Engagements — the CA firm → client edge
# --------------------------------------------------------------------------


class ClientCreateRequest(BaseModel):
    """Engage an existing organization, or create one and engage it.

    Exactly one of ``client_org_id`` and ``organization`` is required. Two
    shapes in one schema because the two cases are the same action from the
    firm's point of view — "take on this client" — and splitting them into two
    endpoints would make the UI decide first whether the company is already on
    the platform, which it cannot know.
    """

    client_org_id: int | None = None
    organization: OrganizationCreateRequest | None = None

    engagement_type: EngagementType = EngagementType.FULL_COMPLIANCE
    start_date: date | None = None
    end_date: date | None = None
    assigned_user_id: int | None = None
    retainer_paise: Paise | None = None
    notes: str | None = Field(default=None, max_length=4000)


class ClientUpdateRequest(BaseModel):
    engagement_type: EngagementType | None = None
    status: EngagementStatus | None = None
    end_date: date | None = None
    assigned_user_id: int | None = None
    retainer_paise: Paise | None = None
    notes: str | None = Field(default=None, max_length=4000)


class ClientResponse(ORMModel):
    """An engagement, with enough of the client organization to render a row."""

    id: int
    ca_firm_id: int
    client_org_id: int
    engagement_type: str
    status: str
    start_date: date
    end_date: date | None = None
    assigned_user_id: int | None = None
    retainer_paise: int | None = None
    notes: str | None = None
    created_at: datetime

    client_organization: OrganizationSummary | None = None
    # Filled by the list endpoint from one grouped query rather than per row;
    # see the router. Null when the caller asked for the cheap listing.
    open_filings: int | None = None
    overdue_filings: int | None = None


class ClientAssignmentRequest(BaseModel):
    """Give one staff member access to one client."""

    user_id: int
    # Caps the access below the user's own role. Null means "whatever their
    # role allows"; it can only narrow, never widen — see
    # :func:`app.core.tenancy.resolve_delegation`.
    granted_role: str | None = None


class ClientAssignmentResponse(ORMModel):
    id: int
    user_id: int
    client_id: int
    client_org_id: int
    granted_role: str | None = None
    created_at: datetime
