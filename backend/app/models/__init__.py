"""Model package.

Every model is imported here, and everything that needs the metadata imports
*this* rather than the individual modules. Alembic's autogenerate compares
``Base.metadata`` against the database: a model that no module has imported is
absent from that metadata, and autogenerate silently proposes dropping its
table. One import list is what keeps that from happening.
"""
from app.models.audit import GENESIS_CHECKSUM, AuditTrail
from app.models.document import Document
from app.models.dpdp import (
    BREACH_NOTIFICATION_HOURS,
    DSR_SLA_DAYS,
    BreachIncident,
    ConsentRecord,
    DataMapEntry,
    DataSubjectRequest,
    PrivacyImpactAssessment,
)
from app.models.enums import (
    PRIVILEGED_ROLES,
    AuditAction,
    DocumentType,
    EngagementStatus,
    EngagementType,
    EntityType,
    FilingStatus,
    Frequency,
    ImpactLevel,
    NotificationChannel,
    NotificationStatus,
    OrgType,
    ParseStatus,
    PlanTier,
    Regulation,
    UserRole,
    role_rank,
)
from app.models.filing import Deadline, Filing
from app.models.notification import Notification, NotificationPreference
from app.models.obligation import ComplianceObligation, OrganizationObligation
from app.models.organization import Client, Organization
from app.models.regulatory import RegulatoryImpact, RegulatoryUpdate
from app.models.template import Template
from app.models.user import ClientAssignment, User

__all__ = [
    "BREACH_NOTIFICATION_HOURS",
    "DSR_SLA_DAYS",
    "GENESIS_CHECKSUM",
    "PRIVILEGED_ROLES",
    "AuditAction",
    "AuditTrail",
    "BreachIncident",
    "Client",
    "ClientAssignment",
    "ComplianceObligation",
    "ConsentRecord",
    "DataMapEntry",
    "DataSubjectRequest",
    "Deadline",
    "Document",
    "DocumentType",
    "EngagementStatus",
    "EngagementType",
    "EntityType",
    "Filing",
    "FilingStatus",
    "Frequency",
    "ImpactLevel",
    "Notification",
    "NotificationChannel",
    "NotificationPreference",
    "NotificationStatus",
    "Organization",
    "OrganizationObligation",
    "OrgType",
    "ParseStatus",
    "PlanTier",
    "PrivacyImpactAssessment",
    "Regulation",
    "RegulatoryImpact",
    "RegulatoryUpdate",
    "Template",
    "User",
    "UserRole",
    "role_rank",
]
