"""The filing and document template library (section 4.8)."""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.enums import Regulation
from app.models.mixins import JSONType, SoftDeleteMixin, TimestampMixin


class Template(Base, TimestampMixin, SoftDeleteMixin):
    """A form definition, or the body of a standard letter.

    Two kinds of row share this table because they share every operational
    concern — versioning, activation, tenant ownership, and the fact that a
    regulatory change invalidates both:

    * **Filing templates** carry a field schema in ``template_json`` which the
      filing editor renders and the AI generator fills.
    * **Document templates** carry ``body_template``, a placeholder string for
      board resolutions and regulatory replies.

    ``organization_id`` is nullable for the same reason it is on the
    obligations catalogue: null is a CompliPilot-maintained template that every
    tenant can use, and a value is a CA firm's own.
    """

    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(primary_key=True)

    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )

    code: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    regulation: Mapped[Regulation | None] = mapped_column(
        Enum(Regulation, native_enum=False, length=32), index=True
    )
    filing_type: Mapped[str | None] = mapped_column(String(64), index=True)
    # "filing" or "document". A plain string rather than an enum because the
    # library is expected to grow categories (checklists, certificates) faster
    # than a migration cycle.
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="filing")

    # The field schema: a list of ``{key, label, type, required, help}`` plus
    # optional sections. Rendered by the frontend and used as the JSON contract
    # the LLM is asked to fill, which is what keeps a generated draft
    # structurally valid rather than free prose.
    template_json: Mapped[dict | None] = mapped_column(JSONType)
    body_template: Mapped[str | None] = mapped_column(Text)

    # Monotonic per ``code``. A filing keeps a pointer to the template row it
    # was drafted against, so re-opening an old filing renders the fields that
    # existed when it was written rather than today's.
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Free-text guidance shown beside the form, and the statutory reference the
    # template implements.
    instructions: Mapped[str | None] = mapped_column(Text)
    statutory_reference: Mapped[str | None] = mapped_column(String(255))

    metadata_json: Mapped[dict | None] = mapped_column(JSONType)

    __table_args__ = (
        # One row per (owner, code, version). New versions are inserted, never
        # updated in place — an amended form is a different document, and a
        # filing drafted against version 2 must not silently become version 3.
        UniqueConstraint(
            "organization_id", "code", "version", "deleted_at", name="uq_templates_code_version"
        ),
        Index("ix_templates_lookup", "code", "is_active", "version"),
        Index("ix_templates_regulation_active", "regulation", "is_active"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Template {self.code} v{self.version}{' system' if self.is_system else ''}>"

    @property
    def fields(self) -> list[dict]:
        """The declared fields, or an empty list for a body-only template."""
        raw = (self.template_json or {}).get("fields")
        return list(raw) if isinstance(raw, list) else []
