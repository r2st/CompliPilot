"""Enumerations shared by the models, schemas and rule engine.

Every one of these is a :class:`enum.StrEnum`: members compare equal to their
stored string, and — the part that matters — ``str(member)`` yields the *value*
rather than ``"ClassName.MEMBER"``.

That second property is load-bearing, not cosmetic. These enums are stringified
in three places where the result is compared or hashed rather than merely
displayed: :func:`role_rank` looks a role up in a dict, the audit chain hashes
``str(action)`` into every checksum, and the JWT carries ``str(role)`` as a
claim. A plain ``(str, Enum)`` returns ``"UserRole.ADMIN"`` from ``str()`` under
Python 3.11, which made every one of those silently wrong — and made
``role_rank`` return ``-1`` for *both* sides of a permission comparison, so
``at_least()`` answered True for every role against every requirement. The
whole RBAC layer was open. ``StrEnum`` is what closes it; see
``tests/test_enums.py``, which asserts the property directly rather than
trusting the base class to keep it.

The values are the wire format. Renaming one is a migration and an API break,
so they are written as the regulator writes them wherever a regulator has an
opinion (``GSTR-3B``, ``MGT-7``), and as lowercase snake_case where nobody
does.

Note that the *database* stores the member name, not the value: SQLAlchemy's
``Enum`` type persists ``.name`` by default, so the column holds ``ADMIN``
while the API emits ``admin``. Both directions go through SQLAlchemy and
Pydantic respectively, so nothing has to convert by hand — but a hand-written
SQL query has to match on the name.
"""
from __future__ import annotations

from enum import StrEnum


class OrgType(StrEnum):
    """Which side of the CA firm → client hierarchy an organization sits on.

    A ``ca_firm`` has ``clients`` rows pointing at the companies it acts for; a
    ``company`` is the entity that actually owes the filings. A company with no
    CA firm above it is the direct-SMB case and is fully supported — the
    hierarchy is optional, not required.
    """

    CA_FIRM = "ca_firm"
    COMPANY = "company"


class EntityType(StrEnum):
    """Legal form of a company, which is what most obligations key off.

    A private limited company owes MGT-7 and AOC-4; an LLP owes Form 8 and
    Form 11 instead; a proprietorship owes neither. Applicability rules in the
    obligations catalogue are expressed against these values.
    """

    PRIVATE_LIMITED = "private_limited"
    PUBLIC_LIMITED = "public_limited"
    ONE_PERSON_COMPANY = "one_person_company"
    LLP = "llp"
    PARTNERSHIP = "partnership"
    PROPRIETORSHIP = "proprietorship"
    TRUST = "trust"
    SOCIETY = "society"
    SECTION_8 = "section_8"
    FOREIGN_COMPANY = "foreign_company"
    HUF = "huf"


class Regulation(StrEnum):
    """The eight regulatory domains of the coverage matrix (section 5)."""

    GST = "gst"
    INCOME_TAX = "income_tax"
    RBI = "rbi"
    SEBI = "sebi"
    MCA = "mca"
    FEMA = "fema"
    LABOR = "labor"
    DPDP = "dpdp"


class Frequency(StrEnum):
    """How often an obligation recurs.

    ``EVENT_BASED`` is not a schedule: FC-GPR is due 30 days after an
    allotment, and there is no allotment until there is one. Those obligations
    produce a filing when the event is recorded, never from the calendar sweep.
    """

    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    HALF_YEARLY = "half_yearly"
    ANNUAL = "annual"
    EVENT_BASED = "event_based"
    ONE_TIME = "one_time"


class FilingStatus(StrEnum):
    """Lifecycle of one filing instance.

    The legal transitions live in :mod:`app.services.filing_workflow`; this is
    only the vocabulary. ``DRAFT`` covers both a blank filing and an
    AI-generated one awaiting the human review that section 4.2 requires —
    generation never advances the status by itself.
    """

    NOT_STARTED = "not_started"
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    # Filed after the statutory due date. Kept distinct from SUBMITTED because
    # a late filing usually carries a penalty and always carries a question at
    # the next audit.
    LATE_FILED = "late_filed"
    NOT_APPLICABLE = "not_applicable"


class ImpactLevel(StrEnum):
    """Priority band for a regulatory update or its per-client impact."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class UserRole(StrEnum):
    """The four default roles of section 8.2, most privileged first.

    Ordering is meaningful and :func:`role_rank` depends on it: permission
    checks are "at least this role", so inserting a member in the wrong place
    silently widens access.
    """

    ADMIN = "admin"
    COMPLIANCE_MANAGER = "compliance_manager"
    STAFF = "staff"
    READ_ONLY = "read_only"


_ROLE_ORDER: dict[str, int] = {
    UserRole.ADMIN: 3,
    UserRole.COMPLIANCE_MANAGER: 2,
    UserRole.STAFF: 1,
    UserRole.READ_ONLY: 0,
}

# TOTP is mandatory for these two (section 8.2).
PRIVILEGED_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.ADMIN, UserRole.COMPLIANCE_MANAGER}
)


def role_rank(role: UserRole | str) -> int:
    """Numeric rank of a role; higher is more privileged.

    Unknown values rank ``-1`` rather than raising. A role string that reached
    the database from an older release should deny access, not 500 — failing
    closed is the whole point of the check that calls this.
    """
    return _ROLE_ORDER.get(str(role), -1)


class NotificationChannel(StrEnum):
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    SMS = "sms"
    IN_APP = "in_app"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    # The channel had no credentials configured. Distinct from FAILED: nothing
    # went wrong, the deployment simply has no WhatsApp token, and retrying
    # will not help.
    SKIPPED = "skipped"


class DocumentType(StrEnum):
    REGULATORY_NOTICE = "regulatory_notice"
    CIRCULAR = "circular"
    SHOW_CAUSE_NOTICE = "show_cause_notice"
    FILING_ATTACHMENT = "filing_attachment"
    GENERATED_FILING = "generated_filing"
    CONSENT_RECORD = "consent_record"
    OTHER = "other"


class ParseStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PARSED = "parsed"
    FAILED = "failed"


class AuditAction(StrEnum):
    """What an audit entry records.

    Deliberately coarse. The interesting detail is in the before/after payload;
    the action is what a regulator filters on.
    """

    CREATE = "create"
    UPDATE = "update"
    SOFT_DELETE = "soft_delete"
    READ = "read"
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    SUBMIT = "submit"
    APPROVE = "approve"
    REJECT = "reject"
    GENERATE = "generate"
    EXPORT = "export"
    ROLE_CHANGE = "role_change"
    NOTIFY = "notify"


class EngagementType(StrEnum):
    """What a CA firm was engaged to do for a client."""

    FULL_COMPLIANCE = "full_compliance"
    GST_ONLY = "gst_only"
    AUDIT_ONLY = "audit_only"
    ADVISORY = "advisory"
    CUSTOM = "custom"


class EngagementStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    TERMINATED = "terminated"


class PlanTier(StrEnum):
    """Pricing tiers of section 7.1."""

    SMB = "smb"
    PROFESSIONAL = "professional"
    CA_FIRM = "ca_firm"
    ENTERPRISE = "enterprise"
