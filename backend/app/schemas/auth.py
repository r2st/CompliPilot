"""Request and response bodies for the auth endpoints."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import EntityType, OrgType, UserRole
from app.schemas.common import ORMModel, validate_phone


class RegisterRequest(BaseModel):
    """Sign up an organization and its first Admin in one call.

    One call rather than two because there is no meaningful intermediate state:
    an organization with no users cannot be reached, and a user with no
    organization has no tenant to belong to.
    """

    organization_name: str = Field(min_length=2, max_length=255)
    organization_type: OrgType = OrgType.COMPANY
    entity_type: EntityType | None = None
    state: str | None = Field(default=None, max_length=64)

    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    # Eight is the floor, not a recommendation. The real defence is bcrypt and
    # the lockout window; a length rule that pushes people to "Passw0rd!" buys
    # nothing, so the ceiling is generous enough for a passphrase.
    password: str = Field(min_length=8, max_length=128)
    phone: str | None = None

    _v_phone = field_validator("phone")(validate_phone)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    # Sent on the second leg of a two-step login. Optional here rather than a
    # separate endpoint so a client that already knows the user has TOTP can
    # send both at once and skip a round trip.
    totp_code: str | None = Field(default=None, max_length=10)


class TokenResponse(BaseModel):
    """A completed login."""

    access_token: str
    refresh_token: str
    # Lowercase "bearer" because that is what OAuth2 clients — including the
    # Swagger UI — match on.
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class TotpChallengeResponse(BaseModel):
    """Issued when the password was right but a second factor is required.

    A distinct response shape rather than a 401 with a hint: the client has to
    know to show the code field, and a 401 would send it back to the login
    form it just left.
    """

    totp_required: bool = True
    challenge_token: str
    expires_in: int = 300


class RefreshRequest(BaseModel):
    refresh_token: str


class SwitchClientRequest(BaseModel):
    """Ask for a token scoped to one of the firm's clients.

    ``None`` switches back to the firm's own context, which is how a CA leaves
    a client rather than having to log in again.
    """

    client_org_id: int | None = None


class TotpSetupResponse(BaseModel):
    """The enrolment payload. The secret is shown exactly once."""

    secret: str
    provisioning_uri: str
    # Sent so the client can tell the user this is not yet active — a secret
    # that has been issued but never confirmed does not count as coverage.
    confirmed: bool = False


class TotpConfirmRequest(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class UserResponse(ORMModel):
    id: int
    email: str
    full_name: str
    phone: str | None = None
    role: UserRole
    organization_id: int
    is_active: bool
    has_totp: bool = False
    last_login_at: datetime | None = None
    created_at: datetime


class UserCreateRequest(BaseModel):
    """Invite a colleague. Admin only."""

    email: EmailStr
    full_name: str = Field(min_length=2, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    role: UserRole = UserRole.STAFF
    phone: str | None = None

    _v_phone = field_validator("phone")(validate_phone)


class UserUpdateRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=255)
    phone: str | None = None
    role: UserRole | None = None
    is_active: bool | None = None

    _v_phone = field_validator("phone")(validate_phone)


class SessionContextResponse(BaseModel):
    """What ``/auth/me`` returns: who you are and whose data you are looking at."""

    user: UserResponse
    organization_id: int
    organization_name: str
    home_organization_id: int
    home_organization_name: str
    role: UserRole
    is_delegated: bool
    # Clients this user may switch into. Empty for a company user, and for a
    # CA firm's staff it is only what their assignments grant.
    available_clients: list[ClientSummary] = []


class ClientSummary(BaseModel):
    client_id: int
    organization_id: int
    name: str
    entity_type: EntityType | None = None
    status: str


# Both response models reference a model defined after them.
TokenResponse.model_rebuild()
SessionContextResponse.model_rebuild()
