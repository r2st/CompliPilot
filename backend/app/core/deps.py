"""FastAPI dependencies: the current user, the tenant they act for, and guards.

A route asks for :func:`get_tenant_context` and receives everything it needs to
scope a query. A route that forgets has no ``org_id`` to filter on and fails
loudly rather than quietly reading across tenants — which is the point of
making the context a dependency rather than something read off ``request``.
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.errors import AuthError, ForbiddenError, RateLimitError
from app.core.logging import bind_context
from app.core.rate_limit import check as rate_limit_check
from app.core.security import decode_token
from app.core.tenancy import TenantContext, resolve_delegation
from app.models.enums import PRIVILEGED_ROLES, UserRole
from app.models.organization import Organization
from app.models.user import User

# ``auto_error=False`` so a missing header raises our AuthError with the shared
# envelope rather than FastAPI's bare ``{"detail": ...}``.
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.api_v1_prefix}/auth/login", auto_error=False
)

_INVALID = "Could not validate credentials"


def get_current_user(
    token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    """Resolve the authenticated user from the bearer token."""
    if not token:
        raise AuthError(_INVALID)

    claims = decode_token(token, expected_type="access")
    if claims is None:
        raise AuthError(_INVALID)

    try:
        user_id = int(claims.get("sub", ""))
    except (TypeError, ValueError) as exc:
        raise AuthError(_INVALID) from exc

    user = db.get(User, user_id)
    if user is None or not user.is_active or user.deleted_at is not None:
        raise AuthError(_INVALID)

    # The token carries the organization it was minted for. If the user has
    # since been moved, the token is stale and must not authenticate against
    # the new tenant — otherwise a transfer between organizations silently
    # re-points an outstanding token at data the holder never had.
    if int(claims.get("org", -1)) != user.organization_id:
        raise AuthError(_INVALID)

    # Stashed for the delegation step, which needs the claim and would
    # otherwise have to decode the token a second time.
    user._token_claims = claims  # type: ignore[attr-defined]
    return user


def get_current_organization(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Organization:
    """The user's own organization — their employer, not the client in context."""
    org = db.get(Organization, current_user.organization_id)
    if org is None or org.deleted_at is not None or not org.is_active:
        raise ForbiddenError("Organization is inactive")
    return org


def get_tenant_context(
    request: Request,
    current_user: User = Depends(get_current_user),
    home_org: Organization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> TenantContext:
    """Resolve which organization this request may read and write.

    Without a ``client_org_id`` claim the answer is the user's own
    organization. With one, the user is a CA firm member acting for a client:
    the engagement and the assignment are re-checked here on every request
    rather than trusted from the token, because an engagement can be terminated
    or an assignment revoked while a twelve-hour token is still valid.
    """
    claims = getattr(current_user, "_token_claims", {}) or {}
    client_org_id = claims.get("client_org_id")

    if client_org_id is None:
        ctx = TenantContext(
            user=current_user,
            org_id=current_user.organization_id,
            home_org_id=current_user.organization_id,
            role=current_user.role,
        )
    else:
        if home_org.type != "ca_firm":
            raise ForbiddenError("Only a CA firm may act for a client organization")
        try:
            client, effective_role = resolve_delegation(db, current_user, int(client_org_id))
        except PermissionError as exc:
            raise ForbiddenError(str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise AuthError(_INVALID) from exc

        ctx = TenantContext(
            user=current_user,
            org_id=client.client_org_id,
            home_org_id=current_user.organization_id,
            role=effective_role,
            is_delegated=True,
            client_id=client.id,
        )

    # Rate limiting is applied here rather than in middleware because this is
    # the first point at which the organization is known, and section 6.1 wants
    # the limit per organization.
    result = rate_limit_check(f"org:{ctx.org_id}")
    if not result.allowed:
        raise RateLimitError(
            "Rate limit exceeded for this organization",
            details={"retry_after": result.retry_after, "limit": result.limit},
        )

    bind_context(org_id=ctx.org_id, user_id=ctx.user_id)
    request.state.tenant = ctx
    return ctx


def require_role(minimum: UserRole) -> Callable[..., TenantContext]:
    """Dependency factory: refuse a request below *minimum*.

    Returns a dependency rather than being one, so a route reads
    ``Depends(require_role(UserRole.COMPLIANCE_MANAGER))`` and the requirement
    is visible in the signature instead of buried in the body.
    """

    def _guard(ctx: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        if not ctx.at_least(minimum):
            raise ForbiddenError(
                f"This action requires the {minimum.value} role",
                details={"required_role": minimum.value, "your_role": str(ctx.role)},
            )
        return ctx

    return _guard


# The three guards used across the API. Named constants rather than repeated
# factory calls so every route that needs "a writer" is spelled the same way.
require_writer = require_role(UserRole.STAFF)
require_manager = require_role(UserRole.COMPLIANCE_MANAGER)
require_admin = require_role(UserRole.ADMIN)


def require_second_factor(ctx: TenantContext = Depends(get_tenant_context)) -> TenantContext:
    """Refuse a privileged user who has not enrolled TOTP.

    Section 8.2 makes TOTP mandatory for Admin and Compliance Manager. Enforced
    at the point of use rather than only at login, because a user promoted to
    Admin mid-session holds a token minted before the requirement applied to
    them — and the promotion must not wait for that token to expire.

    The enrolment endpoints themselves must not depend on this, or a newly
    promoted admin could never enrol.
    """
    if not settings.require_totp_for_privileged_roles:
        return ctx
    if ctx.role in PRIVILEGED_ROLES and not ctx.user.has_totp:
        raise ForbiddenError(
            "Two-factor authentication must be enabled for this role",
            details={"enrol_at": f"{settings.api_v1_prefix}/auth/totp/setup"},
            )
    return ctx


def client_ip(request: Request) -> str:
    """Best available client address, for audit entries and anonymous limiting.

    ``X-Forwarded-For`` is trusted only in production, where the app sits
    behind Caddy and the header is set by it. Trusting it in development would
    let anyone spoof their own address into the audit trail, and trusting it
    unconditionally in production still means trusting the *first* hop — which
    is why only the leftmost entry Caddy appended is read.
    """
    if settings.is_production:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "unknown")[:45]


# Re-exported so routes import one name for the common 401 rather than
# constructing it, which keeps the message identical everywhere.
UNAUTHORIZED_STATUS = status.HTTP_401_UNAUTHORIZED
