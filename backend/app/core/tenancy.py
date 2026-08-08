"""Tenant scoping — the single place a query learns which organization it may see.

The rule from section 2.3 is "every query includes organization_id filtering",
and the way to keep a rule like that is to make the compliant thing shorter
than the non-compliant one. Every read goes through :func:`scoped`, which
applies the tenant filter *and* the soft-delete filter together, because those
two are always both wanted and forgetting either is a silent bug: forget the
first and you serve another firm's filings, forget the second and you serve
records someone deleted.

:class:`TenantContext` is what a route receives. It carries both organizations
in play — the one the user belongs to, and the one they are acting for — and
answers the only question a route needs to ask: which ``organization_id`` do I
filter on.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.enums import UserRole, role_rank
from app.models.organization import Client
from app.models.user import ClientAssignment, User


@dataclass(frozen=True)
class TenantContext:
    """Who is asking, and on whose behalf.

    ``org_id`` is the tenant whose data the request may touch — the answer to
    "what do I filter on". For a company user it is their own organization. For
    a CA firm user who has switched into a client, it is the *client's*
    organization, because the filings being read belong to the client.

    ``home_org_id`` stays the user's employer throughout. The two are kept
    separate rather than collapsed because they are used for different things:
    ``org_id`` scopes data, ``home_org_id`` scopes the audit trail entry —
    an action a CA firm took on a client's behalf belongs in *both* histories,
    and recording only one of them loses the evidence of who actually acted.
    """

    user: User
    org_id: int
    home_org_id: int
    role: UserRole
    # True when the user is acting on a client rather than on their employer.
    is_delegated: bool = False
    client_id: int | None = None

    @property
    def user_id(self) -> int:
        return self.user.id

    def at_least(self, role: UserRole) -> bool:
        """Whether the effective role meets *role*.

        Uses the effective role, which for a delegated request may be capped
        below the user's own by the assignment's ``granted_role``.
        """
        return role_rank(self.role) >= role_rank(role)

    def can_write(self) -> bool:
        return self.at_least(UserRole.STAFF)


def scoped(model, ctx: TenantContext | int, *, include_deleted: bool = False) -> Select:
    """``SELECT * FROM model`` filtered to one tenant and to live rows.

    Accepts either a context or a bare ``organization_id`` so that background
    tasks — which have no user — can use the same helper as the routes rather
    than hand-rolling the filter and getting it subtly different.

    ``include_deleted`` exists for the audit and restore paths. It is a keyword
    argument on purpose: at a call site, ``scoped(Filing, ctx, True)`` would be
    unreadable, and the reader of a query that returns deleted rows should have
    to see the word.
    """
    org_id = ctx if isinstance(ctx, int) else ctx.org_id
    stmt = select(model).where(model.organization_id == org_id)
    if not include_deleted and hasattr(model, "deleted_at"):
        stmt = stmt.where(model.deleted_at.is_(None))
    return stmt


def catalogue_scoped(model, ctx: TenantContext | int, *, include_deleted: bool = False) -> Select:
    """As :func:`scoped`, for the tables where ``organization_id`` is nullable.

    The obligations catalogue and the template library hold system rows shared
    by every tenant (``organization_id IS NULL``) alongside each tenant's own.
    A tenant sees the union; they never see another tenant's.
    """
    org_id = ctx if isinstance(ctx, int) else ctx.org_id
    stmt = select(model).where(
        (model.organization_id.is_(None)) | (model.organization_id == org_id)
    )
    if not include_deleted and hasattr(model, "deleted_at"):
        stmt = stmt.where(model.deleted_at.is_(None))
    return stmt


def accessible_client_org_ids(db: Session, user: User) -> list[int]:
    """Client organizations this user may switch into.

    A firm's Admin and Compliance Manager reach every client of the firm.
    Staff and Read-Only reach only what :class:`ClientAssignment` grants them —
    section 4.5's "staff can only see assigned clients".

    Returns organization ids rather than ``Client`` rows because that is what
    the caller checks a token claim against, and because the same list is used
    for the consolidated dashboard's ``IN (...)``.
    """
    if role_rank(user.role) >= role_rank(UserRole.COMPLIANCE_MANAGER):
        rows = db.execute(
            select(Client.client_org_id).where(
                Client.ca_firm_id == user.organization_id,
                Client.deleted_at.is_(None),
                Client.status == "active",
            )
        ).scalars()
        return list(rows)

    rows = db.execute(
        select(ClientAssignment.client_org_id)
        .join(Client, Client.id == ClientAssignment.client_id)
        .where(
            ClientAssignment.user_id == user.id,
            ClientAssignment.deleted_at.is_(None),
            Client.deleted_at.is_(None),
            Client.ca_firm_id == user.organization_id,
            Client.status == "active",
        )
    ).scalars()
    return list(rows)


def resolve_delegation(
    db: Session, user: User, client_org_id: int
) -> tuple[Client, UserRole]:
    """Check a delegated access and return the engagement plus the effective role.

    Raises :class:`PermissionError`, which the dependency layer turns into a
    403. Raising rather than returning ``None`` keeps the caller from treating
    "not permitted" as "no client found" and answering 404 — the difference
    matters, because a 404 here would tell a CA firm whether a company they are
    not engaged by exists on the platform.
    """
    client = db.execute(
        select(Client).where(
            Client.ca_firm_id == user.organization_id,
            Client.client_org_id == client_org_id,
            Client.deleted_at.is_(None),
        )
    ).scalar_one_or_none()

    if client is None or client.status != "active":
        raise PermissionError("No active engagement with that client")

    if role_rank(user.role) >= role_rank(UserRole.COMPLIANCE_MANAGER):
        return client, user.role

    assignment = db.execute(
        select(ClientAssignment).where(
            ClientAssignment.user_id == user.id,
            ClientAssignment.client_id == client.id,
            ClientAssignment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()

    if assignment is None:
        raise PermissionError("You are not assigned to that client")

    # The assignment can only narrow, never widen: a Staff user handed an
    # ``admin`` grant on a client would otherwise gain rights on that client
    # they do not hold at their own firm.
    if assignment.granted_role is not None and role_rank(assignment.granted_role) < role_rank(
        user.role
    ):
        return client, assignment.granted_role
    return client, user.role
