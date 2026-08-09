"""Document upload, parsing and extraction bodies."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.models.enums import DocumentType, ParseStatus, Regulation
from app.schemas.common import ORMModel


class DocumentSummary(ORMModel):
    """A document in a list. Deliberately without ``parsed_content``.

    A parsed 40-page assessment order is a large string, and returning it on
    every row of a list turns a document index into a several-megabyte
    response. It is on the detail schema instead.
    """

    id: int
    organization_id: int
    type: DocumentType
    title: str
    original_filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    content_hash: str | None = None
    filing_id: int | None = None
    regulation: Regulation | None = None
    parse_status: ParseStatus
    parsed_at: datetime | None = None
    extraction_method: str | None = None
    extracted_deadline: date | None = None
    extracted_summary: str | None = None
    uploaded_by_id: int | None = None
    created_at: datetime


class DocumentResponse(DocumentSummary):
    parsed_content: str | None = None
    parse_error: str | None = None
    extracted_json: dict | None = None
    metadata_json: dict | None = None
    updated_at: datetime


class DocumentUpdateRequest(BaseModel):
    """Correct a document's metadata, or what the parser got wrong.

    ``extracted_deadline`` is editable because the parser is a language model
    reading a scanned notice, and a wrong deadline on a show-cause reply is the
    single most expensive mistake it can make. A human overriding it is the
    designed path, not a workaround.
    """

    title: str | None = Field(default=None, min_length=1, max_length=512)
    type: DocumentType | None = None
    regulation: Regulation | None = None
    filing_id: int | None = None
    extracted_deadline: date | None = None
    extracted_summary: str | None = Field(default=None, max_length=8000)
    metadata_json: dict | None = None


class DocumentUploadResponse(BaseModel):
    """The answer to an upload.

    ``deduplicated`` says the bytes were already here under another row, and
    the id points at the existing one. Worth reporting: a CA who uploads the
    same notice from two mailboxes should be told it is the same notice rather
    than left with two rows and two parse jobs.
    """

    document: DocumentResponse
    deduplicated: bool = False
    parse_queued: bool = False


class ExtractionResult(BaseModel):
    """What the parser pulled out of a regulatory notice.

    The shape the LLM is asked to return, and the shape stored in
    ``extracted_json``. Declared as a schema rather than left as a free dict so
    that a model returning something else fails validation at the boundary
    instead of writing an unusable payload into the column.
    """

    authority: str | None = None
    reference_no: str | None = None
    notice_date: date | None = None
    deadline: date | None = None
    summary: str | None = None
    requirements: list[str] = []
    suggested_actions: list[str] = []
    regulation: Regulation | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
