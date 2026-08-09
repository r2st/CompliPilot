"""Uploaded and generated documents, and what the parser extracted from them."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.enums import DocumentType, ParseStatus, Regulation
from app.models.mixins import (
    JSONType,
    OrgScopedMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
)


class Document(Base, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    """A file belonging to one organization.

    Covers both directions: notices the client received and uploaded, and
    filings CompliPilot generated for them. Both are evidence at an audit, so
    both are retained under the same soft-delete rule.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)

    type: Mapped[DocumentType] = mapped_column(
        Enum(DocumentType, native_enum=False, length=32),
        default=DocumentType.OTHER,
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    # As uploaded. Kept separate from ``storage_path`` because the stored name
    # is a generated one — a user-supplied name is a path traversal waiting to
    # happen, and two clients both uploading "notice.pdf" must not collide.
    original_filename: Mapped[str | None] = mapped_column(String(512))
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column()
    # SHA-256 of the bytes. Lets a re-upload of the same notice be recognised
    # rather than re-parsed, and proves at an audit that the file on disk is
    # the file that was received.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    # Optional links to what the document is about.
    filing_id: Mapped[int | None] = mapped_column(
        ForeignKey("filings.id", ondelete="SET NULL"), index=True
    )
    regulation: Mapped[Regulation | None] = mapped_column(
        Enum(Regulation, native_enum=False, length=32), index=True
    )

    # --- Parsing (section 4.3) -------------------------------------------
    parse_status: Mapped[ParseStatus] = mapped_column(
        Enum(ParseStatus, native_enum=False, length=32),
        default=ParseStatus.PENDING,
        nullable=False,
        index=True,
    )
    parsed_content: Mapped[str | None] = mapped_column(Text)
    parse_error: Mapped[str | None] = mapped_column(Text)
    parsed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Which route produced the text: "pdf_text", "docx", "html", "ocr". Worth
    # recording because an OCR result deserves less trust than an extracted
    # text layer, and the UI says so.
    extraction_method: Mapped[str | None] = mapped_column(String(32))

    # What the LLM pulled out: issuing authority, reference, the requirements
    # asserted, the deadline, and the suggested response actions.
    extracted_json: Mapped[dict | None] = mapped_column(JSONType)
    # Promoted out of ``extracted_json`` because the calendar needs to index
    # it: a show-cause notice with a 15-day reply window is a deadline.
    extracted_deadline: Mapped[date | None] = mapped_column(Date, index=True)
    extracted_summary: Mapped[str | None] = mapped_column(Text)

    uploaded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSONType)

    __table_args__ = (
        Index("ix_documents_org_type", "organization_id", "type"),
        # The parse worker's queue query.
        Index("ix_documents_parse_queue", "parse_status", "created_at"),
        Index("ix_documents_org_created", "organization_id", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Document id={self.id} {self.type} {self.parse_status}>"
