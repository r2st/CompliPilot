"""Parsing uploaded documents (section 4.3).

The pipeline is three steps, and the middle one is the reason they are separate:

1. Extract the text (:mod:`app.services.extraction`) — mechanical, right answer.
2. Ask a model what the document requires (:mod:`app.services.llm`) — a guess,
   recorded as one.
3. Store both, so a wrong answer at step 2 can be told apart from a failure at
   step 1 by looking at the row.

**Every outcome is recorded.** A document that cannot be parsed becomes
``FAILED`` with the reason on the row, never silently ``PENDING`` forever. That
is what makes the parse queue drain: the worker's query selects ``PENDING``, so
a document that stayed pending after a crash would be retried indefinitely
while one that errored and was left alone would vanish from view.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy import select

from app.celery_app import celery_app
from app.core.config import settings
from app.models.document import Document
from app.models.enums import AuditAction, ParseStatus
from app.models.mixins import utcnow
from app.services import audit as audit_service
from app.services import extraction, llm
from app.tasks.base import SYSTEM_ACTOR, task_session

logger = logging.getLogger(__name__)

# Documents parsed per scheduled sweep. The queue is normally drained by the
# per-upload task; this bound is for the catch-up after an outage.
PARSE_BATCH = 50

# What the analyser must return. Enforced by ``complete_json`` rather than
# hoped for, so the caller can index ``result.data`` without defending each
# access.
_REQUIRED = {"summary", "issuing_authority", "requirements", "deadline", "confidence"}

_ANALYSIS_PROMPT = """\
Analyse this Indian regulatory document and return JSON with exactly these keys:

- "summary": 2-4 sentences, plain English, stating what this document is and \
what it requires of the recipient.
- "issuing_authority": the body that issued it (e.g. "CBIC", "MCA", "Income \
Tax Department"), or null if not stated.
- "reference_no": the document's own reference or notice number, or null.
- "document_type": one of "notice", "circular", "show_cause_notice", \
"assessment_order", "acknowledgement", "other".
- "regulation": one of "gst", "income_tax", "rbi", "sebi", "mca", "fema", \
"labour", "dpdp", or null if unclear.
- "requirements": an array of strings, each a specific action the recipient \
must take. Empty array if the document is purely informational.
- "deadline": the date by which a response is required, as "YYYY-MM-DD", or \
null if none is stated. Do NOT infer a date that is not in the document.
- "monetary_amounts": an array of {{"label": string, "amount_inr": number}} \
for any demand, penalty or refund stated. Empty array if none.
- "confidence": integer 0-100, how confident you are in this extraction.

Return ONLY the JSON object.

DOCUMENT:
{content}
"""


def _parse_date(value) -> date | None:
    """Read the model's date field, or give up cleanly.

    Never guesses. A deadline is the field most likely to be hallucinated and
    the most expensive to get wrong — it lands on a compliance calendar — so
    anything that is not an unambiguous ISO date is dropped rather than
    coerced.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        logger.info("Unparseable deadline from model: %r", value)
        return None


def _analyse(document: Document, text: str) -> llm.LLMResult | None:
    """Ask the model what the document requires. ``None`` when unconfigured."""
    if not llm.is_configured():
        logger.info("Skipping analysis of document %s: no API key", document.id)
        return None

    return llm.complete_json(
        _ANALYSIS_PROMPT.format(content=extraction.clip_for_model(text)),
        required_keys=_REQUIRED,
    )


def _store_analysis(document: Document, result: llm.LLMResult) -> None:
    data = result.data or {}
    document.extracted_json = {
        **data,
        # Provenance alongside the content, not in a separate column, so an
        # export of the extraction carries the model that produced it.
        "_model": result.model,
        "_tokens": result.total_tokens,
    }
    document.extracted_summary = data.get("summary")
    document.extracted_deadline = _parse_date(data.get("deadline"))


def parse_document(db, document: Document) -> dict:
    """Run the pipeline for one document. Flushes; the caller commits.

    Separated from the task body so a route can parse synchronously on upload
    when the user is waiting, using the same code the worker runs.
    """
    document.parse_status = ParseStatus.PROCESSING
    db.flush()

    storage_root = settings.upload_dir
    path = document.storage_path
    # Stored paths are relative to the upload root so the directory can be
    # moved between deployments; an absolute one is honoured as-is.
    full_path = path if path.startswith("/") else f"{storage_root.rstrip('/')}/{path}"

    try:
        text, method = extraction.extract_text(full_path, mime_type=document.mime_type)
    except extraction.ScannedDocument as exc:
        # No text layer. The vision model reads the image directly, but only
        # if one is configured; otherwise this is an honest failure rather than
        # a document recorded as empty.
        logger.info("Document %s is a scan: %s", document.id, exc)
        document.parse_status = ParseStatus.FAILED
        document.parse_error = (
            f"{exc}. A scanned document needs OCR or the vision model, "
            "neither of which is available for this file."
        )
        document.parsed_at = utcnow()
        db.flush()
        return {"document_id": document.id, "status": "failed", "reason": "scanned"}
    except extraction.ExtractionError as exc:
        document.parse_status = ParseStatus.FAILED
        document.parse_error = str(exc)[:2000]
        document.parsed_at = utcnow()
        db.flush()
        return {"document_id": document.id, "status": "failed", "reason": "unreadable"}

    document.parsed_content = text
    document.extraction_method = method

    try:
        result = _analyse(document, text)
    except llm.UpstreamError as exc:
        # The text extraction succeeded and is worth keeping. The document is
        # PARSED — the mechanical step worked — with the analysis failure
        # recorded so a retry can pick it up without re-reading the file.
        logger.warning("Analysis failed for document %s: %s", document.id, exc)
        document.parse_status = ParseStatus.PARSED
        document.parse_error = f"Text extracted; analysis unavailable: {exc}"
        document.parsed_at = utcnow()
        db.flush()
        return {"document_id": document.id, "status": "parsed", "analysed": False}

    if result is not None:
        _store_analysis(document, result)

    document.parse_status = ParseStatus.PARSED
    document.parse_error = None
    document.parsed_at = utcnow()
    db.flush()

    return {
        "document_id": document.id,
        "status": "parsed",
        "analysed": result is not None,
        "method": method,
        "chars": len(text),
    }


@celery_app.task(name="app.tasks.document_tasks.parse_document_task")
def parse_document_task(document_id: int) -> dict:
    """Parse one document, queued when it is uploaded."""
    with task_session() as db:
        document = db.get(Document, document_id)
        if document is None or document.deleted_at is not None:
            logger.warning("parse_document_task: no live document %s", document_id)
            return {"document_id": document_id, "found": False}

        try:
            outcome = parse_document(db, document)
            audit_service.record(
                db,
                organization_id=document.organization_id,
                action=AuditAction.UPDATE,
                entity_type="document",
                entity_id=document.id,
                actor_label=SYSTEM_ACTOR,
                summary=f"Parsed document: {outcome['status']}",
                after={
                    "parse_status": str(document.parse_status),
                    "extraction_method": document.extraction_method,
                },
            )
            db.commit()
            return outcome
        except Exception:
            db.rollback()
            # Recorded on its own transaction: the rollback above discarded the
            # PROCESSING write, and a document left in that state is invisible
            # to both the queue (which selects PENDING) and to anyone looking
            # for failures.
            failed = db.get(Document, document_id)
            if failed is not None:
                failed.parse_status = ParseStatus.FAILED
                failed.parse_error = "Parsing crashed; see worker logs"
                failed.parsed_at = utcnow()
                db.commit()
            logger.exception("Parsing crashed for document %s", document_id)
            raise


@celery_app.task(name="app.tasks.document_tasks.sweep_pending_documents")
def sweep_pending_documents(limit: int = PARSE_BATCH) -> dict:
    """Catch-up pass over documents still waiting to be parsed.

    The per-upload task is the normal path; this exists because that task can
    be lost — the broker was down when the upload happened, or a worker died
    between the ack and the body. Without a sweep those documents wait forever.
    """
    parsed = 0
    failed = 0

    with task_session() as db:
        pending = (
            db.execute(
                select(Document.id)
                .where(
                    Document.parse_status == ParseStatus.PENDING,
                    Document.deleted_at.is_(None),
                )
                .order_by(Document.created_at)
                .limit(limit)
            )
            .scalars()
            .all()
        )

    # Each document gets its own task and its own session, so one unreadable
    # upload cannot take the sweep down with it.
    for document_id in pending:
        try:
            outcome = parse_document_task(document_id)
            if outcome.get("status") == "parsed":
                parsed += 1
            else:
                failed += 1
        except Exception:  # noqa: BLE001 - already logged and recorded
            failed += 1

    logger.info("Document sweep: %s parsed, %s failed", parsed, failed)
    return {"parsed": parsed, "failed": failed, "considered": len(pending)}
