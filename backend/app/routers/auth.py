"""Registration, login, TOTP enrolment, client-context switching, and user admin."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import (
    client_ip,
    get_current_user,
    get_tenant_context,
    require_admin,
)
from app.core.errors import AuthError, ConflictError, ForbiddenError, NotFoundError
from app.core.rate_limit import check as rate_limit_check
from app.core.security import (
    create_access_token,
    create_refresh_token,
    create_totp_challenge_token,
    decode_token,
    generate_totp_secret,
    hash_password,
    load_totp_secret,
    store_totp_secret,
    totp_provisioning_uri,
    verify_password,
    verify_totp,
)
from app.core.tenancy import TenantContext, accessible_client_org_ids, resolve_delegation
from app.models.enums import PRIVILEGED_ROLES, AuditAction, OrgType, UserRole
from app.models.organization import Client, Organization
from app.models.user import User
from app.schemas.auth import (
    ClientSummary,
    LoginRequest,
    PasswordChangeRequest,
    RefreshRequest,
    RegisterRequest,
    SessionContextResponse,
    SwitchClientRequest,
    TokenResponse,
    TotpChallengeResponse,
    TotpConfirmRequest,
    TotpSetupResponse,
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)
from app.schemas.common import MessageResponse
from app.services import audit as audit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Failed logins allowed before the account is locked, and for how long. Five is
# well above a typo and far below a useful guessing budget; fifteen minutes is
# long enough to make automation pointless and short enough that a locked-out
# CA is not blocked through a filing deadline.
_MAX_FAILED_LOGINS = 5
_LOCKOUT_MINUTES = 15

# The same message for every login failure. Distinguishing "no such account"
# from "wrong password" hands over a list of who banks with which CA firm.
_LOGIN_FAILED = "Incorrect email or password"


def _user_response(user: User) -> UserResponse:
    """Serialize a user, filling the computed ``has_totp``."""
    return UserResponse.model_validate({**user.__dict__, "has_totp": user.has_totp})


def _issue_tokens(user: User, *, client_org_id: int | None = None) -> TokenResponse:
    access = create_access_token(
        user.id,
        org_id=user.organization_id,
        role=str(user.role),
        client_org_id=client_org_id,
    )
    return TokenResponse(
        access_token=access,
        refresh_token=create_refresh_token(user.id, org_id=user.organization_id),
        expires_in=settings.access_token_expire_minutes * 60,
        user=_user_response(user),
    )


def _find_user(db: Session, email: str) -> User | None:
    """Look up a live user by email, case-insensitively.

    Email is unique per organization, not globally, so this can in principle
    match more than one row — a person with an account at their firm and at a
    company. The first is taken and the rest are reachable by switching
    context; a login form with no organization field has no better answer, and
    asking for one would leak which organizations an address belongs to.
    """
    return db.execute(
        select(User)
        .where(func.lower(User.email) == email.lower(), User.deleted_at.is_(None))
        .order_by(User.id)
        .limit(1)
    ).scalar_one_or_none()


def _register_failure(db: Session, user: User | None) -> None:
    """Count a failed attempt and lock the account once it is over the line."""
    if user is None:
        return
    user.failed_login_count += 1
    if user.failed_login_count >= _MAX_FAILED_LOGINS:
        user.locked_until = datetime.now(UTC) + timedelta(minutes=_LOCKOUT_MINUTES)
        logger.warning("Locked user %s after %d failures", user.id, user.failed_login_count)
    db.commit()


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register an organization and its first admin",
)
def register(payload: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    """Create an organization and the Admin who owns it.

    Rate limited per IP rather than per organization, because at this point
    there is no organization yet and registration is the endpoint someone
    would script to enumerate email addresses.
    """
    limit = rate_limit_check(f"register:{client_ip(request)}", limit=10, window_seconds=3600)
    if not limit.allowed:
        raise ForbiddenError("Too many registration attempts; try again later")

    org = Organization(
        name=payload.organization_name,
        type=payload.organization_type,
        entity_type=payload.entity_type,
        state=payload.state,
        contact_email=str(payload.email),
    )
    db.add(org)
    db.flush()

    user = User(
        organization_id=org.id,
        email=str(payload.email).lower(),
        full_name=payload.full_name,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        role=UserRole.ADMIN,
    )
    db.add(user)
    db.flush()

    audit_service.record(
        db,
        organization_id=org.id,
        action=AuditAction.CREATE,
        entity_type="organization",
        entity_id=org.id,
        user_id=user.id,
        actor_label=user.full_name,
        ip_address=client_ip(request),
        request_id=getattr(request.state, "request_id", None),
        after={"name": org.name, "type": str(org.type)},
        summary=f"Organization {org.name} registered",
    )
    db.commit()
    db.refresh(user)

    return _issue_tokens(user)


@router.post(
    "/login",
    response_model=TokenResponse | TotpChallengeResponse,
    summary="Exchange credentials for tokens",
)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """Password login, with a TOTP second leg where the role requires one.

    Returns either a full token pair or a challenge. The two shapes are
    distinguishable by the ``totp_required`` field, which is only ever present
    on the challenge.
    """
    ip = client_ip(request)
    # Per-IP, because the organization is unknown until the credentials are
    # checked and this is the endpoint that gets sprayed.
    limit = rate_limit_check(f"login:{ip}", limit=20, window_seconds=300)
    if not limit.allowed:
        raise AuthError("Too many login attempts; try again shortly")

    user = _find_user(db, str(payload.email))

    if user is not None and user.locked_until is not None:
        if user.locked_until > datetime.now(UTC):
            raise AuthError(
                "Account temporarily locked after repeated failed attempts",
            )
        # The window has passed: clear it so the counter starts fresh rather
        # than the next single failure re-locking the account.
        user.locked_until = None
        user.failed_login_count = 0

    # Runs even when ``user`` is None — see verify_password's docstring for why
    # the timing of the two paths has to match.
    if not verify_password(payload.password, user.password_hash if user else None):
        if user is not None:
            audit_service.record(
                db,
                organization_id=user.organization_id,
                action=AuditAction.LOGIN_FAILED,
                entity_type="user",
                entity_id=user.id,
                user_id=user.id,
                actor_label=user.email,
                ip_address=ip,
                request_id=getattr(request.state, "request_id", None),
                summary="Failed login: incorrect password",
            )
        _register_failure(db, user)
        raise AuthError(_LOGIN_FAILED)

    assert user is not None  # verify_password only succeeds with a real hash

    if not user.is_active:
        raise AuthError("This account has been deactivated")

    needs_totp = user.has_totp or (
        settings.require_totp_for_privileged_roles and user.role in PRIVILEGED_ROLES
    )

    if needs_totp:
        if not user.has_totp:
            # Privileged, mandated, and not yet enrolled. Refusing outright
            # would lock the only Admin out of the organization they just
            # registered, so the challenge token is issued and the enrolment
            # endpoints accept it.
            return TotpChallengeResponse(
                challenge_token=create_totp_challenge_token(
                    user.id, org_id=user.organization_id
                )
            )
        if not payload.totp_code:
            return TotpChallengeResponse(
                challenge_token=create_totp_challenge_token(
                    user.id, org_id=user.organization_id
                )
            )
        if not verify_totp(load_totp_secret(user.totp_secret), payload.totp_code):
            _register_failure(db, user)
            raise AuthError("Incorrect authentication code")

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = datetime.now(UTC)

    audit_service.record(
        db,
        organization_id=user.organization_id,
        action=AuditAction.LOGIN,
        entity_type="user",
        entity_id=user.id,
        user_id=user.id,
        actor_label=user.full_name,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
        summary="Signed in",
    )
    db.commit()
    db.refresh(user)

    return _issue_tokens(user)


@router.post("/token", response_model=TokenResponse, include_in_schema=False)
def login_form(
    form: OAuth2PasswordRequestForm = Depends(),
    request: Request = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
):
    """The OAuth2 password-form variant, for the Swagger UI's Authorize button.

    Hidden from the schema because it is a development affordance and not part
    of the documented API. It refuses a user who needs TOTP rather than
    returning a challenge, since the form flow has nowhere to put the code.
    """
    result = login(
        LoginRequest(email=form.username, password=form.password), request, db
    )
    if isinstance(result, TotpChallengeResponse):
        raise AuthError("This account requires two-factor authentication; use /auth/login")
    return result


@router.post("/refresh", response_model=TokenResponse, summary="Exchange a refresh token")
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    """Mint a new access token.

    The role is read from the database rather than from the token, which is
    what makes a demotion take effect at the next refresh rather than at the
    next login. The client context is deliberately dropped: a refresh returns
    the user to their own organization, and switching back into a client
    re-runs the engagement checks.
    """
    claims = decode_token(payload.refresh_token, expected_type="refresh")
    if claims is None:
        raise AuthError("Invalid or expired refresh token")

    try:
        user = db.get(User, int(claims.get("sub", "")))
    except (TypeError, ValueError) as exc:
        raise AuthError("Invalid refresh token") from exc

    if user is None or not user.is_active or user.deleted_at is not None:
        raise AuthError("Invalid refresh token")
    if int(claims.get("org", -1)) != user.organization_id:
        raise AuthError("Invalid refresh token")

    return _issue_tokens(user)


@router.get("/me", response_model=SessionContextResponse, summary="Current session")
def me(ctx: TenantContext = Depends(get_tenant_context), db: Session = Depends(get_db)):
    """Who you are, whose data you are looking at, and where else you can go."""
    org = db.get(Organization, ctx.org_id)
    home = db.get(Organization, ctx.home_org_id)
    if org is None or home is None:  # pragma: no cover - FK guarantees both
        raise NotFoundError("Organization not found")

    clients: list[ClientSummary] = []
    if home.type == OrgType.CA_FIRM:
        allowed = set(accessible_client_org_ids(db, ctx.user))
        if allowed:
            rows = db.execute(
                select(Client, Organization)
                .join(Organization, Organization.id == Client.client_org_id)
                .where(
                    Client.ca_firm_id == home.id,
                    Client.client_org_id.in_(allowed),
                    Client.deleted_at.is_(None),
                )
                .order_by(Organization.name)
            ).all()
            clients = [
                ClientSummary(
                    client_id=client.id,
                    organization_id=client_org.id,
                    name=client_org.name,
                    entity_type=client_org.entity_type,
                    status=client.status,
                )
                for client, client_org in rows
            ]

    return SessionContextResponse(
        user=_user_response(ctx.user),
        organization_id=org.id,
        organization_name=org.name,
        home_organization_id=home.id,
        home_organization_name=home.name,
        role=ctx.role,
        is_delegated=ctx.is_delegated,
        available_clients=clients,
    )


@router.post(
    "/switch-client",
    response_model=TokenResponse,
    summary="Get a token scoped to one client organization",
)
def switch_client(
    payload: SwitchClientRequest,
    request: Request,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """Section 2.3's context switch, as a token exchange.

    A new token rather than a session flag: the tenant a request acts on is a
    signed claim, so it cannot be changed by editing a header, and an audit
    entry can state which context an action was taken in without trusting the
    client to say.
    """
    home = db.get(Organization, ctx.home_org_id)
    if home is None or home.type != OrgType.CA_FIRM:
        raise ForbiddenError("Only a CA firm may act for a client organization")

    if payload.client_org_id is None:
        return _issue_tokens(ctx.user)

    try:
        client, _role = resolve_delegation(db, ctx.user, payload.client_org_id)
    except PermissionError as exc:
        raise ForbiddenError(str(exc)) from exc

    audit_service.record(
        db,
        organization_id=client.client_org_id,
        action=AuditAction.READ,
        entity_type="organization",
        entity_id=client.client_org_id,
        user_id=ctx.user_id,
        actor_label=ctx.user.full_name,
        ip_address=client_ip(request),
        request_id=getattr(request.state, "request_id", None),
        summary=f"{home.name} switched into this client's context",
    )
    db.commit()

    return _issue_tokens(ctx.user, client_org_id=client.client_org_id)


# --- TOTP ----------------------------------------------------------------


@router.post("/totp/setup", response_model=TotpSetupResponse, summary="Begin TOTP enrolment")
def totp_setup(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Issue a secret and its provisioning URI.

    Depends on ``get_current_user`` rather than the tenant context, so it does
    not go through :func:`require_second_factor` — an Admin who must enrol
    before they can do anything would otherwise be unable to reach the
    enrolment endpoint.

    Re-enrolling replaces an unconfirmed secret. A *confirmed* one is refused:
    silently rotating a working second factor on an unauthenticated-by-TOTP
    request would be a way to strip it.
    """
    if current_user.has_totp:
        raise ConflictError(
            "Two-factor authentication is already enabled; disable it first to re-enrol"
        )

    secret = generate_totp_secret()
    current_user.totp_secret = store_totp_secret(secret)
    current_user.totp_confirmed_at = None
    db.commit()

    return TotpSetupResponse(
        secret=secret,
        provisioning_uri=totp_provisioning_uri(secret, email=current_user.email),
    )


@router.post("/totp/confirm", response_model=MessageResponse, summary="Confirm TOTP enrolment")
def totp_confirm(
    payload: TotpConfirmRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Prove the authenticator works before the secret counts as active."""
    secret = load_totp_secret(current_user.totp_secret)
    if not secret:
        raise ConflictError("Start enrolment with /auth/totp/setup first")
    if not verify_totp(secret, payload.code):
        raise AuthError("Incorrect authentication code")

    current_user.totp_confirmed_at = datetime.now(UTC)
    audit_service.record(
        db,
        organization_id=current_user.organization_id,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=current_user.id,
        user_id=current_user.id,
        actor_label=current_user.full_name,
        ip_address=client_ip(request),
        summary="Enabled two-factor authentication",
    )
    db.commit()
    return MessageResponse(message="Two-factor authentication enabled")


@router.post("/totp/disable", response_model=MessageResponse, summary="Turn TOTP off")
def totp_disable(
    payload: TotpConfirmRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove the second factor, after proving possession of it.

    Refused outright for a privileged role while the deployment mandates TOTP:
    section 8.2 makes it mandatory, and an Admin who could turn it off has an
    optional second factor with extra steps.
    """
    if (
        settings.require_totp_for_privileged_roles
        and current_user.role in PRIVILEGED_ROLES
    ):
        raise ForbiddenError(
            "Two-factor authentication is mandatory for this role and cannot be disabled"
        )
    if not verify_totp(load_totp_secret(current_user.totp_secret), payload.code):
        raise AuthError("Incorrect authentication code")

    current_user.totp_secret = None
    current_user.totp_confirmed_at = None
    audit_service.record(
        db,
        organization_id=current_user.organization_id,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=current_user.id,
        user_id=current_user.id,
        actor_label=current_user.full_name,
        ip_address=client_ip(request),
        summary="Disabled two-factor authentication",
    )
    db.commit()
    return MessageResponse(message="Two-factor authentication disabled")


@router.post("/password", response_model=MessageResponse, summary="Change your password")
def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise AuthError("Current password is incorrect")

    current_user.password_hash = hash_password(payload.new_password)
    audit_service.record(
        db,
        organization_id=current_user.organization_id,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=current_user.id,
        user_id=current_user.id,
        actor_label=current_user.full_name,
        ip_address=client_ip(request),
        # No before/after: the hashes are redacted anyway, and recording that
        # the password changed is the whole of the auditable fact.
        summary="Changed password",
    )
    db.commit()
    return MessageResponse(message="Password updated")


# --- User administration --------------------------------------------------


@router.get("/users", response_model=list[UserResponse], summary="List users in your organization")
def list_users(
    ctx: TenantContext = Depends(get_tenant_context), db: Session = Depends(get_db)
):
    """Users of the *home* organization.

    Deliberately ``home_org_id`` rather than ``org_id``: a CA firm member
    working inside a client's context is still asking about their own
    colleagues, and a firm has no business listing a client's user accounts.
    """
    users = (
        db.execute(
            select(User)
            .where(User.organization_id == ctx.home_org_id, User.deleted_at.is_(None))
            .order_by(User.full_name)
        )
        .scalars()
        .all()
    )
    return [_user_response(u) for u in users]


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a user to your organization",
)
def create_user(
    payload: UserCreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_admin),
    db: Session = Depends(get_db),
):
    existing = db.execute(
        select(User).where(
            User.organization_id == ctx.home_org_id,
            func.lower(User.email) == str(payload.email).lower(),
            User.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("A user with that email already exists in this organization")

    user = User(
        organization_id=ctx.home_org_id,
        email=str(payload.email).lower(),
        full_name=payload.full_name,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.flush()

    audit_service.record(
        db,
        organization_id=ctx.home_org_id,
        action=AuditAction.CREATE,
        entity_type="user",
        entity_id=user.id,
        user_id=ctx.user_id,
        actor_label=ctx.user.full_name,
        ip_address=client_ip(request),
        after={"email": user.email, "role": str(user.role)},
        summary=f"Added user {user.email} as {user.role}",
    )
    db.commit()
    db.refresh(user)
    return _user_response(user)


@router.patch("/users/{user_id}", response_model=UserResponse, summary="Update a user")
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Change a colleague's name, phone, role or active flag.

    A role change is called out separately in the audit entry because section
    8.2 requires it: "All role changes are logged in the audit trail".
    """
    user = db.execute(
        select(User).where(
            User.id == user_id,
            User.organization_id == ctx.home_org_id,
            User.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("User not found")

    before = {"role": str(user.role), "is_active": user.is_active, "full_name": user.full_name}
    role_changed = payload.role is not None and payload.role != user.role

    if role_changed and user.id == ctx.user_id:
        # An admin demoting themselves can strand an organization with no
        # admin at all, and there is no self-service way back.
        raise ForbiddenError("You cannot change your own role")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)

    audit_service.record(
        db,
        organization_id=ctx.home_org_id,
        action=AuditAction.ROLE_CHANGE if role_changed else AuditAction.UPDATE,
        entity_type="user",
        entity_id=user.id,
        user_id=ctx.user_id,
        actor_label=ctx.user.full_name,
        ip_address=client_ip(request),
        before=before,
        after={"role": str(user.role), "is_active": user.is_active, "full_name": user.full_name},
        summary=(
            f"Changed {user.email}'s role from {before['role']} to {user.role}"
            if role_changed
            else f"Updated user {user.email}"
        ),
    )
    db.commit()
    db.refresh(user)
    return _user_response(user)


@router.delete(
    "/users/{user_id}", response_model=MessageResponse, summary="Deactivate a user"
)
def delete_user(
    user_id: int,
    request: Request,
    ctx: TenantContext = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Soft delete. The row stays so the audit trail's ``user_id`` still resolves."""
    user = db.execute(
        select(User).where(
            User.id == user_id,
            User.organization_id == ctx.home_org_id,
            User.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("User not found")
    if user.id == ctx.user_id:
        raise ForbiddenError("You cannot remove your own account")

    user.soft_delete()
    user.is_active = False

    audit_service.record(
        db,
        organization_id=ctx.home_org_id,
        action=AuditAction.SOFT_DELETE,
        entity_type="user",
        entity_id=user.id,
        user_id=ctx.user_id,
        actor_label=ctx.user.full_name,
        ip_address=client_ip(request),
        before={"email": user.email, "role": str(user.role)},
        summary=f"Removed user {user.email}",
    )
    db.commit()
    return MessageResponse(message="User removed")
