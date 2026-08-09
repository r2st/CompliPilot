"""Document upload, storage and retrieval (section 4.3).

**Filenames are never trusted.** The user's name is kept in
``original_filename`` for display and nothing else. What lands on disk is a
generated name under a per-organization directory, because ``../../etc/passwd``
is a filename and two clients both uploading ``notice.pdf`` must not collide.

**Content is hashed on the way in.** The SHA-256 makes a re-upload of the same
notice recognisable, so a CA who receives the same order in two mailboxes gets
one document and one parse job rather than two of each. It also proves at an
assessment that the file on disk is the file that was received.
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_tenant_context, require_manager, require_writer
from app.core.errors import NotFoundError, ValidationError
from app.core.tenancy import TenantContext, scoped
from app.models.document import Document
from app.models.enums import AuditAction, DocumentType, ParseStatus, Regulation
from app.models.filing import Filing
from app.routers._helpers import changed_fields, paginate, record, snapshot
from app.schemas.common import MessageResponse, Page
from app.schemas.document import (
    DocumentResponse,
    DocumentSummary,
    DocumentUpdateRequest,
    DocumentUploadResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

# What the parser can actually read. An unsupported type is refused at upload
# rather than accepted and left permanently PENDING, which would look like a
# broken worker rather than a rejected file.
ALLOWED_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "text/html",
        "text/plain",
        "image/png",
        "image/jpeg",
    }
)

# Read in chunks so a 25 MB upload does not become 25 MB of resident memory per
# concurrent request. The limit is enforced as the stream is consumed, so an
# oversized file is rejected partway rather than after it has all arrived.
_CHUNK_BYTES = 1024 * 1024

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _storage_root() -> Path:
    return Path(settings.upload_dir)


def _safe_original_name(name: str | None) -> str | None:
    """The user's filename, reduced to something safe to echo back.

    Kept for display only — it never touches the filesystem. Even so it is
    stripped of path separators and control characters, because it is rendered
    in a browser and in a PDF export, and both have been an injection vector.
    """
    if not name:
        return None
    cleaned = _SAFE_NAME.sub("_", Path(name).name).strip("._")
    return cleaned[:512] or None


def _store(org_id: int, upload: UploadFile) -> tuple[Path, str, int]:
    """Stream an upload to disk. Returns ``(path, sha256, size)``.

    The size limit is checked as the stream is read. Trusting
    ``Content-Length`` would let a client understate it, and reading the whole
    body before checking is the denial of service the limit exists to prevent.
    """
    directory = _storage_root() / str(org_id)
    directory.mkdir(parents=True, exist_ok=True)

    suffix = Path(upload.filename or "").suffix[:16]
    path = directory / f"{uuid.uuid4().hex}{suffix}"

    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("wb") as handle:
            while chunk := upload.file.read(_CHUNK_BYTES):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise ValidationError(
                        f"The file exceeds the {settings.max_upload_mb} MB limit",
                        details={"max_bytes": settings.max_upload_bytes},
                    )
                digest.update(chunk)
                handle.write(chunk)
    except Exception:
        # A partial file left on disk is a file the parse worker would pick up
        # and fail on, and one nobody has a row pointing at to clean up.
        path.unlink(missing_ok=True)
        raise

    if size == 0:
        path.unlink(missing_ok=True)
        raise ValidationError("The uploaded file is empty")

    return path, digest.hexdigest(), size


@router.get("", response_model=Page[DocumentSummary], summary="List documents")
def list_documents(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    type: DocumentType | None = None,
    regulation: Regulation | None = None,
    parse_status: ParseStatus | None = None,
    filing_id: int | None = None,
    has_deadline: bool | None = Query(
        None, description="Only documents the parser found a deadline in"
    ),
    search: str | None = Query(None, max_length=128),
):
    stmt = scoped(Document, ctx)
    if type is not None:
        stmt = stmt.where(Document.type == type)
    if regulation is not None:
        stmt = stmt.where(Document.regulation == regulation)
    if parse_status is not None:
        stmt = stmt.where(Document.parse_status == parse_status)
    if filing_id is not None:
        stmt = stmt.where(Document.filing_id == filing_id)
    if has_deadline is True:
        stmt = stmt.where(Document.extracted_deadline.is_not(None))
    elif has_deadline is False:
        stmt = stmt.where(Document.extracted_deadline.is_(None))
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Document.title.ilike(pattern),
                Document.original_filename.ilike(pattern),
                Document.extracted_summary.ilike(pattern),
            )
        )

    stmt = stmt.order_by(Document.created_at.desc(), Document.id.desc())
    rows, total = paginate(db, stmt, limit=limit, offset=offset)
    return Page[DocumentSummary](
        items=[DocumentSummary.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document",
)
def upload_document(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(None),
    type: DocumentType = Form(DocumentType.OTHER),
    regulation: Regulation | None = Form(None),
    filing_id: int | None = Form(None),
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
):
    """Store a file against this organization and queue it for parsing.

    A file whose bytes are already here returns the existing document with
    ``deduplicated=true`` rather than creating a second row. The uploaded copy
    is discarded — keeping it would leave an orphan on disk that no row points
    at, which is the kind of thing that fills a volume six months later.
    """
    if file.content_type and file.content_type not in ALLOWED_MIME_TYPES:
        raise ValidationError(
            f"{file.content_type} is not a document type this parser can read",
            details={"allowed": sorted(ALLOWED_MIME_TYPES)},
        )

    if filing_id is not None:
        linked = db.execute(
            scoped(Filing, ctx).where(Filing.id == filing_id)
        ).scalar_one_or_none()
        if linked is None:
            raise NotFoundError("No such filing")

    path, content_hash, size = _store(ctx.org_id, file)

    existing = db.execute(
        scoped(Document, ctx).where(Document.content_hash == content_hash)
    ).scalar_one_or_none()
    if existing is not None:
        path.unlink(missing_ok=True)
        return DocumentUploadResponse(
            document=DocumentResponse.model_validate(existing),
            deduplicated=True,
            parse_queued=False,
        )

    original = _safe_original_name(file.filename)
    document = Document(
        organization_id=ctx.org_id,
        type=type,
        title=(title or original or "Untitled document")[:512],
        original_filename=original,
        storage_path=str(path),
        mime_type=file.content_type,
        size_bytes=size,
        content_hash=content_hash,
        filing_id=filing_id,
        regulation=regulation,
        parse_status=ParseStatus.PENDING,
        uploaded_by_id=ctx.user_id,
    )
    db.add(document)
    db.flush()

    record(
        db,
        ctx,
        request,
        action=AuditAction.CREATE,
        entity_type="document",
        entity_id=document.id,
        after={
            "title": document.title,
            "type": str(document.type),
            "size_bytes": size,
            "content_hash": content_hash,
        },
        summary=f"Document uploaded: {document.title}",
    )
    db.commit()
    db.refresh(document)

    return DocumentUploadResponse(
        document=DocumentResponse.model_validate(document),
        deduplicated=False,
        parse_queued=True,
    )


@router.get("/{document_id}", response_model=DocumentResponse, summary="One document")
def get_document(
    document_id: int,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    document = db.execute(
        scoped(Document, ctx).where(Document.id == document_id)
    ).scalar_one_or_none()
    if document is None:
        raise NotFoundError("No such document")
    return DocumentResponse.model_validate(document)


@router.get(
    "/{document_id}/download",
    response_class=FileResponse,
    summary="Download the original file",
)
def download_document(
    document_id: int,
    request: Request,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """Stream the stored bytes back.

    Recorded in the audit trail as a READ. Downloads are the action a
    regulator asks about most often after a leak — "who took a copy of the
    assessment order, and when" — and an access log that lives only in Caddy
    is not part of the tamper-evident chain.
    """
    document = db.execute(
        scoped(Document, ctx).where(Document.id == document_id)
    ).scalar_one_or_none()
    if document is None:
        raise NotFoundError("No such document")

    path = Path(document.storage_path)
    if not path.is_file():
        # The row survives; the bytes did not. Reported as a 404 with a
        # distinct message so this is diagnosable rather than looking like a
        # permissions problem.
        logger.error("Document %s has no file at %s", document.id, path)
        raise NotFoundError("The stored file for this document is missing")

    record(
        db,
        ctx,
        request,
        action=AuditAction.READ,
        entity_type="document",
        entity_id=document.id,
        summary=f"Document downloaded: {document.title}",
    )
    db.commit()

    return FileResponse(
        path,
        media_type=document.mime_type or "application/octet-stream",
        filename=document.original_filename or f"document-{document.id}",
    )


@router.patch(
    "/{document_id}", response_model=DocumentResponse, summary="Correct document metadata"
)
def update_document(
    document_id: int,
    payload: DocumentUpdateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_writer),
    db: Session = Depends(get_db),
):
    """Fix what the parser got wrong, or reclassify a document.

    ``extracted_deadline`` is editable on purpose. The parser is a language
    model reading a scanned notice, and a wrong deadline on a show-cause reply
    is the most expensive mistake it can make — a human overriding it is the
    designed path.
    """
    document = db.execute(
        scoped(Document, ctx).where(Document.id == document_id)
    ).scalar_one_or_none()
    if document is None:
        raise NotFoundError("No such document")

    audited = ("title", "type", "regulation", "filing_id", "extracted_deadline")
    before = snapshot(document, audited)

    changes = payload.model_dump(exclude_unset=True)
    if changes.get("filing_id") is not None:
        linked = db.execute(
            scoped(Filing, ctx).where(Filing.id == changes["filing_id"])
        ).scalar_one_or_none()
        if linked is None:
            raise NotFoundError("No such filing")

    for field, value in changes.items():
        setattr(document, field, value)

    diff = changed_fields(before, snapshot(document, audited))
    if diff:
        record(
            db,
            ctx,
            request,
            action=AuditAction.UPDATE,
            entity_type="document",
            entity_id=document.id,
            before={k: v["from"] for k, v in diff.items()},
            after={k: v["to"] for k, v in diff.items()},
            summary=f"Document {document.id} updated: {', '.join(sorted(diff))}",
        )
    db.commit()
    db.refresh(document)
    return DocumentResponse.model_validate(document)


@router.delete(
    "/{document_id}", response_model=MessageResponse, summary="Delete a document"
)
def delete_document(
    document_id: int,
    request: Request,
    ctx: TenantContext = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Soft delete the row. The stored bytes are kept.

    Deliberate: a regulatory notice is evidence, and a deletion that destroyed
    the file would make the audit entry recording the deletion the only
    remaining trace of what was deleted. Purging the bytes is a retention-policy
    job with its own authorisation, not a side effect of someone tidying a list.
    """
    document = db.execute(
        scoped(Document, ctx).where(Document.id == document_id)
    ).scalar_one_or_none()
    if document is None:
        raise NotFoundError("No such document")

    document.soft_delete()
    record(
        db,
        ctx,
        request,
        action=AuditAction.SOFT_DELETE,
        entity_type="document",
        entity_id=document.id,
        before={"title": document.title, "content_hash": document.content_hash},
        summary=f"Document deleted: {document.title}",
    )
    db.commit()
    return MessageResponse(
        message="Document deleted", detail={"file_retained": True}
    )


@router.get(
    "/deadlines/extracted",
    response_model=list[DocumentSummary],
    summary="Deadlines the parser found in uploaded notices",
)
def extracted_deadlines(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
):
    """Documents carrying a deadline, for the calendar's document layer."""
    stmt = scoped(Document, ctx).where(Document.extracted_deadline.is_not(None))
    if from_date is not None:
        stmt = stmt.where(Document.extracted_deadline >= from_date)
    if to_date is not None:
        stmt = stmt.where(Document.extracted_deadline <= to_date)

    rows = db.execute(stmt.order_by(Document.extracted_deadline)).scalars().all()
    return [DocumentSummary.model_validate(r) for r in rows]
