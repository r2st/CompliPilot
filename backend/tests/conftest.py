"""Test fixtures: an isolated database per test, and the actors that use it.

**Why the environment is set before any app import.** ``app.core.config``
builds its ``Settings`` at import and ``app.core.database`` builds the engine
from it, both at module scope. By the time a test module imports anything from
``app``, those are already decided. Setting the variables here — at the top of
the file pytest loads first — is what points the whole application at SQLite
instead of at a Postgres nobody started.

**Why each test gets a fresh schema rather than a rolled-back transaction.**
The transaction trick is faster and it breaks on the code this suite most needs
to test: :func:`app.services.audit.record` opens a ``begin_nested`` to survive a
sequence collision, and the routes commit. A savepoint-based fixture turns both
into something that behaves differently under test than in production, which is
the one property a test fixture must not have. An in-memory SQLite schema
rebuilds in single-digit milliseconds, so the honest version is also cheap.
"""
from __future__ import annotations

import os

# Must precede every ``app`` import. See the module docstring.
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DEBUG": "true",
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "JWT_SECRET": "test-secret-not-used-anywhere-real-but-long-enough-to-pass",
        # Redis is not started for the suite. The limiter fails open when it
        # cannot reach one, but switching it off keeps a test that fires 300
        # requests from depending on that fallback.
        "RATE_LIMIT_ENABLED": "false",
        # Individual tests turn this back on; leaving it on globally would mean
        # every admin fixture had to enrol TOTP before it could do anything.
        "REQUIRE_TOTP_FOR_PRIVILEGED_ROLES": "false",
        "CELERY_ENABLED": "false",
        "LOG_LEVEL": "WARNING",
    }
)

from collections.abc import Callable, Generator  # noqa: E402
from datetime import date  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

# Every model module must be imported before ``create_all``, or its table is
# absent from the metadata and the test fails on a missing table rather than on
# whatever it was checking. ``app.models`` imports all of them; see its
# docstring.
import app.models  # noqa: E402,F401
from app.core.crypto import fingerprint  # noqa: E402
from app.core.database import Base, SessionLocal, engine, get_db  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.enums import (  # noqa: E402
    EngagementStatus,
    EngagementType,
    EntityType,
    FilingStatus,
    Frequency,
    OrgType,
    Regulation,
    UserRole,
)
from app.models.filing import Deadline, Filing  # noqa: E402
from app.models.obligation import ComplianceObligation  # noqa: E402
from app.models.organization import Client, Organization  # noqa: E402
from app.models.user import ClientAssignment, User  # noqa: E402
from app.services.filing_workflow import ensure_deadline  # noqa: E402

API = "/api/v1"

CRORE = 100 * 100_000 * 100  # ₹1 crore in paise, for the turnover fixtures.


@pytest.fixture(autouse=True)
def _schema() -> Generator[None, None, None]:
    """A fresh, empty schema around every test."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db() -> Generator[Session, None, None]:
    """A session for a test to arrange state with directly."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def app_client(db: Session) -> Generator[TestClient, None, None]:
    """A TestClient whose requests share the test's own session.

    Sharing matters: a test arranges a row, then calls an endpoint that must
    see it. With separate sessions the endpoint would read a connection that
    has not seen the test's uncommitted write, and the test would fail for a
    reason that has nothing to do with the code under test.

    ``raise_server_exceptions=False`` so the registered 500 handler is what a
    test observes, rather than the exception being re-raised into the test and
    bypassing the envelope the frontend actually receives.
    """
    application = create_app()

    def _override() -> Generator[Session, None, None]:
        yield db

    application.dependency_overrides[get_db] = _override
    with TestClient(application, raise_server_exceptions=False) as test_client:
        yield test_client
    application.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Factories
#
# Plain functions rather than a factory library: the models have enough
# required columns that a generic factory needs as much configuration as it
# saves, and an explicit helper is greppable when a column is added.
# --------------------------------------------------------------------------

_email_counter = {"n": 0}


def _next_email(prefix: str) -> str:
    _email_counter["n"] += 1
    return f"{prefix}{_email_counter['n']}@example.test"


def make_org(
    db: Session,
    *,
    name: str = "Acme Manufacturing Pvt Ltd",
    type: OrgType = OrgType.COMPANY,
    entity_type: EntityType | None = EntityType.PRIVATE_LIMITED,
    gstin: str | None = None,
    state: str | None = "Karnataka",
    annual_turnover_paise: int | None = 5 * CRORE,
    employee_count: int | None = 25,
    **kwargs,
) -> Organization:
    """An organization, with the profile fields the applicability engine reads.

    ``gstin`` is fingerprinted here rather than encrypted, because the
    uniqueness constraint and every lookup are on the fingerprint — a test that
    only needs "two orgs must not share a GSTIN" needs this column and not the
    ciphertext.
    """
    org = Organization(
        name=name,
        type=type,
        entity_type=entity_type,
        state=state,
        annual_turnover_paise=annual_turnover_paise,
        employee_count=employee_count,
        gstin=gstin,
        gstin_fingerprint=fingerprint(gstin),
        **kwargs,
    )
    db.add(org)
    db.flush()
    return org


def make_user(
    db: Session,
    org: Organization,
    *,
    role: UserRole = UserRole.ADMIN,
    email: str | None = None,
    full_name: str = "Test User",
    password: str = "correct-horse-battery",
    **kwargs,
) -> User:
    user = User(
        organization_id=org.id,
        email=email or _next_email(str(role).replace("_", "")),
        full_name=full_name,
        password_hash=hash_password(password),
        role=role,
        **kwargs,
    )
    db.add(user)
    db.flush()
    return user


def make_engagement(
    db: Session,
    firm: Organization,
    client_org: Organization,
    *,
    status: EngagementStatus = EngagementStatus.ACTIVE,
    engagement_type: EngagementType = EngagementType.FULL_COMPLIANCE,
    assigned_user_id: int | None = None,
) -> Client:
    """A CA firm's engagement with a client company."""
    engagement = Client(
        ca_firm_id=firm.id,
        client_org_id=client_org.id,
        engagement_type=str(engagement_type),
        status=str(status),
        start_date=date(2026, 4, 1),
        assigned_user_id=assigned_user_id,
    )
    db.add(engagement)
    db.flush()
    return engagement


def make_assignment(
    db: Session,
    user: User,
    engagement: Client,
    *,
    granted_role: UserRole | None = None,
) -> ClientAssignment:
    assignment = ClientAssignment(
        user_id=user.id,
        client_id=engagement.id,
        client_org_id=engagement.client_org_id,
        granted_role=granted_role,
    )
    db.add(assignment)
    db.flush()
    return assignment


_code_counter = {"n": 0}


def make_obligation(
    db: Session,
    *,
    code: str | None = None,
    regulation: Regulation = Regulation.GST,
    filing_type: str | None = "GSTR-3B",
    frequency: Frequency = Frequency.MONTHLY,
    due_day: int | None = 20,
    organization_id: int | None = None,
    **kwargs,
) -> ComplianceObligation:
    """A catalogue obligation.

    ``organization_id`` defaults to None, which is what makes it a *system*
    obligation — the shape almost every test wants, since the catalogue is
    shared and only a tenant's own custom obligations carry an owner.
    """
    _code_counter["n"] += 1
    obligation = ComplianceObligation(
        organization_id=organization_id,
        regulation=regulation,
        code=code or f"test.obligation.{_code_counter['n']}",
        title=kwargs.pop("title", "Monthly GST return"),
        filing_type=filing_type,
        frequency=frequency,
        due_day=due_day,
        **kwargs,
    )
    db.add(obligation)
    db.flush()
    return obligation


def make_filing(
    db: Session,
    org: Organization,
    obligation: ComplianceObligation,
    *,
    period_key: str = "2026-07",
    due_date: date = date(2026, 8, 20),
    status: FilingStatus = FilingStatus.NOT_STARTED,
    **kwargs,
) -> Filing:
    filing = Filing(
        organization_id=org.id,
        obligation_id=obligation.id,
        regulation=obligation.regulation,
        filing_type=obligation.filing_type,
        period_key=period_key,
        due_date=due_date,
        status=status,
        **kwargs,
    )
    db.add(filing)
    db.flush()
    return filing


def make_deadline(db: Session, filing: Filing, **kwargs) -> Deadline:
    """The reminder-state row for a filing.

    Goes through ``ensure_deadline`` so a test's deadline is built exactly the
    way the generator builds one, rather than by a second construction path
    that could drift from it.
    """
    deadline = ensure_deadline(db, filing)
    for key, value in kwargs.items():
        setattr(deadline, key, value)
    db.flush()
    return deadline


def token_for(user: User, *, client_org_id: int | None = None) -> str:
    return create_access_token(
        user.id,
        org_id=user.organization_id,
        role=str(user.role),
        client_org_id=client_org_id,
    )


def auth(user: User, *, client_org_id: int | None = None) -> dict[str, str]:
    """Authorization header for *user*, optionally acting for a client."""
    return {"Authorization": f"Bearer {token_for(user, client_org_id=client_org_id)}"}


# --------------------------------------------------------------------------
# Composed fixtures for the two shapes the product serves
# --------------------------------------------------------------------------


@pytest.fixture
def company(db: Session) -> Organization:
    """A direct SMB — a company with no CA firm above it."""
    return make_org(db)


@pytest.fixture
def company_admin(db: Session, company: Organization) -> User:
    return make_user(db, company, role=UserRole.ADMIN, full_name="Priya Sharma")


@pytest.fixture
def company_staff(db: Session, company: Organization) -> User:
    return make_user(db, company, role=UserRole.STAFF, full_name="Rahul Verma")


@pytest.fixture
def company_reader(db: Session, company: Organization) -> User:
    return make_user(db, company, role=UserRole.READ_ONLY, full_name="Anita Rao")


@pytest.fixture
def other_company(db: Session) -> Organization:
    """A second, unrelated tenant. Every isolation test needs one."""
    return make_org(db, name="Rival Industries Pvt Ltd", state="Maharashtra")


@pytest.fixture
def other_admin(db: Session, other_company: Organization) -> User:
    return make_user(db, other_company, role=UserRole.ADMIN, full_name="Vikram Singh")


@pytest.fixture
def ca_firm(db: Session) -> Organization:
    return make_org(
        db,
        name="Sharma & Associates",
        type=OrgType.CA_FIRM,
        entity_type=EntityType.PARTNERSHIP,
        annual_turnover_paise=2 * CRORE,
    )


@pytest.fixture
def firm_admin(db: Session, ca_firm: Organization) -> User:
    return make_user(db, ca_firm, role=UserRole.ADMIN, full_name="CA Meera Sharma")


@pytest.fixture
def firm_staff(db: Session, ca_firm: Organization) -> User:
    return make_user(db, ca_firm, role=UserRole.STAFF, full_name="Junior Associate")


@pytest.fixture
def engagement(db: Session, ca_firm: Organization, company: Organization) -> Client:
    return make_engagement(db, ca_firm, company)


@pytest.fixture
def seeded(db: Session) -> None:
    """The system catalogue and template library, loaded.

    A fixture rather than autouse: most tests do not need 200 obligations, and
    the ones that do should say so.
    """
    from app.data.seed import seed_all

    seed_all(db, commit=False)
    db.flush()


@pytest.fixture
def as_user(app_client: TestClient) -> Callable[..., Callable]:
    """Bind a request helper to one user, so tests read as prose.

    ``get = as_user(admin); get("GET", "/filings")`` rather than repeating the
    header dict on every call.
    """

    def _bind(user: User, *, client_org_id: int | None = None):
        headers = auth(user, client_org_id=client_org_id)

        def _request(method: str, path: str, **kwargs):
            merged = {**headers, **kwargs.pop("headers", {})}
            return app_client.request(method, f"{API}{path}", headers=merged, **kwargs)

        return _request

    return _bind
