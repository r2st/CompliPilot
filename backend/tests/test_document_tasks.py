"""The two scheduled entry points around the document parse pipeline.

:func:`app.tasks.document_tasks.parse_document` — the pipeline itself — is
covered in ``test_documents_api.py``. What is left, and what is here, is the
scaffolding around it: the task queued when a document is uploaded, and the
catch-up sweep that exists because that task can be lost.

Both are about a single property: **a document must never come to rest in a
state nothing looks at.** The sweep's query selects ``PENDING``, so a document
stuck in ``PROCESSING`` after a crash is invisible to the retry path *and* to
anyone searching for failures — it simply sits there, parsed by nobody,
reported by nothing. It is the one outcome worse than a failure, because a
failure at least appears on the row with a reason next to it.

That makes the crash path the interesting one. It has to write ``FAILED`` on a
*new* transaction, because the rollback that preceded it discarded the
``PROCESSING`` write along with everything else.

These tests drive the Celery task functions directly rather than through a
broker. They open their own sessions via ``task_session``, so anything a test
arranges has to be committed first — the same discipline the real worker
imposes, since it genuinely cannot see another session's uncommitted rows.
"""
from __future__ import annotations

import pytest

from app.models.document import Document
from app.models.enums import ParseStatus
from app.services import extraction
from app.tasks import document_tasks
from tests.conftest import make_document

TEXT = b"A demand under s.73 of the CGST Act."


@pytest.fixture(autouse=True)
def upload_root(tmp_path, monkeypatch):
    """Point uploads at a per-test directory.

    Autouse here, unlike in the API tests: every test in this module reaches
    the pipeline, and one that forgot the fixture would write into whatever
    ``upload_dir`` is configured in the environment.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    """No API key, so the analysis step is skipped.

    The analyser is tested in ``test_llm.py`` and its integration in
    ``test_documents_api.py``. Leaving it configurable here would mean these
    tests passed or failed on whether a key happened to be in the environment.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "openrouter_api_key", "")


def readable(db, org, upload_root, *, name="notice.txt", content=TEXT, **kwargs):
    """A document whose bytes are on disk and whose row is committed.

    Committed because the task opens its own session: a document only flushed
    into the test's transaction does not exist as far as the worker can see,
    and the task would correctly report it missing.
    """
    directory = upload_root / str(org.id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)

    document = make_document(
        db, org, storage_path=str(path), mime_type="text/plain", **kwargs
    )
    db.commit()
    return document


def reread(db, document) -> Document:
    """The row as it stands after a task committed on its own session."""
    db.expire_all()
    return db.get(Document, document.id)


# --------------------------------------------------------------------------
# parse_document_task
# --------------------------------------------------------------------------


class TestParseDocumentTask:
    def test_it_parses_and_commits(self, db, company, upload_root):
        document = readable(db, company, upload_root)

        outcome = document_tasks.parse_document_task(document.id)

        assert outcome["status"] == "parsed"
        assert reread(db, document).parse_status == ParseStatus.PARSED

    def test_the_work_survives_the_test_rolling_back(self, db, company, upload_root):
        """The task owns its transaction.

        A worker that left the parse uncommitted would look identical in the
        returned dict and would have done nothing at all.
        """
        document = readable(db, company, upload_root)

        document_tasks.parse_document_task(document.id)

        db.rollback()
        assert reread(db, document).parsed_content

    def test_a_missing_document_is_reported_not_raised(self, db, company):
        """The row was hard-deleted, or the id never existed.

        Raising would put the task into Celery's retry loop for a document that
        will never appear, and ``task_acks_late`` means it would be redelivered
        rather than dropped.
        """
        outcome = document_tasks.parse_document_task(9999)

        assert outcome == {"document_id": 9999, "found": False}

    def test_a_soft_deleted_document_is_left_alone(self, db, company, upload_root):
        """Somebody deleted the upload between the enqueue and the worker
        picking it up. Parsing it anyway would repopulate a row the user
        believes is gone."""
        document = readable(db, company, upload_root)
        document.soft_delete()
        db.commit()

        outcome = document_tasks.parse_document_task(document.id)

        assert outcome["found"] is False
        assert reread(db, document).parse_status == ParseStatus.PENDING

    def test_an_unreadable_file_is_a_recorded_failure_not_a_crash(
        self, db, company, upload_root
    ):
        """The file vanished from storage. That is the document's problem, not
        the worker's, so it is recorded on the row and the task returns."""
        document = make_document(db, company, storage_path="/nonexistent/gone.pdf")
        db.commit()

        outcome = document_tasks.parse_document_task(document.id)

        assert outcome["status"] == "failed"
        row = reread(db, document)
        assert row.parse_status == ParseStatus.FAILED
        assert row.parse_error

    def test_the_parse_is_recorded_in_the_audit_chain(self, db, company, upload_root):
        """A document's parsed content is what a filing gets drafted from, so
        "when was this read, and by what" has to be answerable."""
        from app.models.audit import AuditTrail

        document = readable(db, company, upload_root)

        document_tasks.parse_document_task(document.id)

        db.expire_all()
        entry = (
            db.query(AuditTrail)
            .filter(AuditTrail.entity_type == "document")
            .order_by(AuditTrail.id.desc())
            .first()
        )
        assert entry is not None
        # Stored as a string: the column is String(64) so it can hold the id of
        # an entity that is not keyed by an integer.
        assert entry.entity_id == str(document.id)
        assert entry.organization_id == company.id

    def test_the_entry_says_the_scheduler_did_it(self, db, company, upload_root):
        """Not whichever admin happens to be first in the table. An audit trail
        that attributed automated work to a person is worse than one that
        omitted it."""
        from app.models.audit import AuditTrail
        from app.tasks.base import SYSTEM_ACTOR

        document = readable(db, company, upload_root)

        document_tasks.parse_document_task(document.id)

        db.expire_all()
        entry = (
            db.query(AuditTrail)
            .filter(AuditTrail.entity_type == "document")
            .order_by(AuditTrail.id.desc())
            .first()
        )
        assert entry.actor_label == SYSTEM_ACTOR
        assert entry.user_id is None


class TestWhenParsingCrashes:
    """An exception the pipeline did not anticipate.

    Not an unreadable file — that is handled and recorded. This is the case
    where something genuinely unexpected happens partway through, after the
    document has already been written as ``PROCESSING``.
    """

    @staticmethod
    def _explode(monkeypatch):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("the extractor segfaulted")

        monkeypatch.setattr(extraction, "extract_text", _boom)

    def test_the_exception_reaches_celery(self, db, company, upload_root, monkeypatch):
        """So the task is retried and the failure appears in the worker's error
        rate, rather than being swallowed into a success."""
        document = readable(db, company, upload_root)
        self._explode(monkeypatch)

        with pytest.raises(RuntimeError):
            document_tasks.parse_document_task(document.id)

    def test_the_document_is_left_failed_not_processing(
        self, db, company, upload_root, monkeypatch
    ):
        """The property this whole path exists for.

        ``PROCESSING`` is invisible to the sweep, which selects ``PENDING``, and
        invisible to anyone looking for failures. A document left there is
        parsed by nobody and reported by nothing.
        """
        document = readable(db, company, upload_root)
        self._explode(monkeypatch)

        with pytest.raises(RuntimeError):
            document_tasks.parse_document_task(document.id)

        row = reread(db, document)
        assert row.parse_status == ParseStatus.FAILED
        assert row.parsed_at is not None

    def test_the_failure_is_committed_on_its_own_transaction(
        self, db, company, upload_root, monkeypatch
    ):
        """The rollback before it discarded the PROCESSING write along with
        everything else, so this cannot ride on the same transaction."""
        document = readable(db, company, upload_root)
        self._explode(monkeypatch)

        with pytest.raises(RuntimeError):
            document_tasks.parse_document_task(document.id)

        db.rollback()
        assert reread(db, document).parse_status == ParseStatus.FAILED

    def test_the_row_says_where_to_look(self, db, company, upload_root, monkeypatch):
        """The exception itself is in the worker log. The row carries the
        pointer, because the person who finds the document is not the person
        reading the log."""
        document = readable(db, company, upload_root)
        self._explode(monkeypatch)

        with pytest.raises(RuntimeError):
            document_tasks.parse_document_task(document.id)

        assert "logs" in (reread(db, document).parse_error or "")


# --------------------------------------------------------------------------
# sweep_pending_documents
# --------------------------------------------------------------------------


class TestPendingSweep:
    def test_it_parses_what_the_upload_task_missed(self, db, company, upload_root):
        """The broker was down when the upload happened, or a worker died
        between the ack and the body. Without the sweep those wait forever."""
        readable(db, company, upload_root, name="one.txt")
        readable(db, company, upload_root, name="two.txt")

        result = document_tasks.sweep_pending_documents()

        assert result == {"parsed": 2, "failed": 0, "considered": 2}

    def test_an_already_parsed_document_is_not_reconsidered(
        self, db, company, upload_root
    ):
        """It selects PENDING. Re-reading every document every sweep would mean
        the analysis bill grew with the archive rather than with the uploads."""
        readable(db, company, upload_root, parse_status=ParseStatus.PARSED)

        assert document_tasks.sweep_pending_documents()["considered"] == 0

    def test_a_failed_document_is_not_retried_forever(self, db, company, upload_root):
        """A file that cannot be read will not become readable. Retrying it on
        every sweep would spend the batch on the same broken document and
        starve the ones behind it."""
        readable(db, company, upload_root, parse_status=ParseStatus.FAILED)

        assert document_tasks.sweep_pending_documents()["considered"] == 0

    def test_a_soft_deleted_document_is_not_swept(self, db, company, upload_root):
        document = readable(db, company, upload_root)
        document.soft_delete()
        db.commit()

        assert document_tasks.sweep_pending_documents()["considered"] == 0

    def test_the_oldest_wait_is_served_first(self, db, company, upload_root):
        """A backlog drains in the order it formed. Newest-first would leave the
        documents that have already waited longest waiting indefinitely
        whenever uploads outpace the batch."""
        first = readable(db, company, upload_root, name="first.txt")
        second = readable(db, company, upload_root, name="second.txt")

        document_tasks.sweep_pending_documents(limit=1)

        assert reread(db, first).parse_status == ParseStatus.PARSED
        assert reread(db, second).parse_status == ParseStatus.PENDING

    def test_the_batch_is_bounded(self, db, company, upload_root):
        """A sweep that took the whole backlog would hold a worker for as long
        as the outage lasted."""
        for n in range(3):
            readable(db, company, upload_root, name=f"doc{n}.txt")

        assert document_tasks.sweep_pending_documents(limit=2)["considered"] == 2

    def test_it_covers_every_tenant(self, db, company, other_company, upload_root):
        """A single global queue, not a sweep per organization. The documents
        are unrelated to each other and parsing is not tenant-scoped work."""
        mine = readable(db, company, upload_root, name="mine.txt")
        theirs = readable(db, other_company, upload_root, name="theirs.txt")

        document_tasks.sweep_pending_documents()

        assert reread(db, mine).parse_status == ParseStatus.PARSED
        assert reread(db, theirs).parse_status == ParseStatus.PARSED

    def test_an_empty_queue_is_not_an_error(self, db, company):
        assert document_tasks.sweep_pending_documents() == {
            "parsed": 0,
            "failed": 0,
            "considered": 0,
        }


class TestOneBadDocumentDoesNotStopTheSweep:
    """The reason each document gets its own task and its own session.

    A sweep that shared one transaction across the batch would lose every
    document after the first unreadable one — and the backlog it was draining
    would still be there in the morning.
    """

    def test_an_unreadable_document_is_counted_and_passed_over(
        self, db, company, upload_root
    ):
        make_document(db, company, storage_path="/nonexistent/gone.pdf")
        good = readable(db, company, upload_root, name="good.txt")

        result = document_tasks.sweep_pending_documents()

        assert result == {"parsed": 1, "failed": 1, "considered": 2}
        assert reread(db, good).parse_status == ParseStatus.PARSED

    def test_a_crash_does_not_take_the_sweep_down(
        self, db, company, upload_root, monkeypatch
    ):
        """The per-document task re-raises so Celery sees it. The sweep is the
        one place that must not, or a single poisoned row stops the drain.
        """
        doomed = readable(db, company, upload_root, name="doomed.txt")
        survivor = readable(db, company, upload_root, name="survivor.txt")
        real_extract = extraction.extract_text

        def _boom_on_doomed(path, **kwargs):
            if "doomed" in str(path):
                raise RuntimeError("the extractor segfaulted")
            return real_extract(path, **kwargs)

        monkeypatch.setattr(extraction, "extract_text", _boom_on_doomed)

        result = document_tasks.sweep_pending_documents()

        assert result == {"parsed": 1, "failed": 1, "considered": 2}
        assert reread(db, survivor).parse_status == ParseStatus.PARSED
        assert reread(db, doomed).parse_status == ParseStatus.FAILED
