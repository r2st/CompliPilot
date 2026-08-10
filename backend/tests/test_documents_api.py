"""Document upload, retrieval, and the parse pipeline behind it.

Two properties in this router are security properties rather than features, and
they are the reason the file leads with them:

* **the user's filename never reaches the filesystem.** What lands on disk is a
  generated name under a per-organization directory, so ``../../etc/passwd`` is
  stored as a uuid and two clients uploading ``notice.pdf`` do not collide. The
  tests assert both halves — the stored path is generated, and the name that
  *is* echoed back has been stripped;
* **the size limit is enforced as the stream is read.** Trusting
  ``Content-Length`` would let a client understate it, so the test posts a body
  larger than the limit and asserts nothing is left behind on disk.

The parse pipeline is tested through :func:`parse_document` directly rather than
through the Celery task, matching how the deadline sweeps are tested: the task
wrapper is three lines of session handling and the pipeline is where the
decisions are. The decision worth protecting is that *every* outcome is
recorded — a document that cannot be read becomes FAILED with the reason on the
row, never silently PENDING, because the worker's queue selects PENDING and a
document left there would be retried forever.
"""
from __future__ import annotations

import io
from datetime import date

import pytest

from app.models.document import Document
from app.models.enums import DocumentType, ParseStatus, Regulation, UserRole
from app.services import extraction, llm
from app.tasks import document_tasks
from tests.conftest import (
    API,
    auth,
    make_document,
    make_filing,
    make_obligation,
    make_user,
)

PDF_MIME = "application/pdf"


@pytest.fixture(autouse=True)
def upload_root(tmp_path, monkeypatch):
    """Point uploads at a per-test directory.

    ``settings.upload_dir`` is read inside ``_storage_root()`` on each call
    rather than captured at import, so patching the setting is enough — and it
    keeps a test that writes files from leaving them in the repo.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    return tmp_path


def upload(client, user, *, content=b"%PDF-1.4 fake", filename="notice.pdf", **data):
    return client.post(
        f"{API}/documents",
        headers=auth(user),
        files={"file": (filename, io.BytesIO(content), data.pop("mime", PDF_MIME))},
        data=data,
    )


class TestUpload:
    def test_an_upload_creates_a_document_and_queues_a_parse(
        self, app_client, db, company, company_admin
    ):
        response = upload(app_client, company_admin, title="Assessment order")

        assert response.status_code == 201
        body = response.json()
        assert body["deduplicated"] is False
        assert body["parse_queued"] is True
        document = body["document"]
        assert document["title"] == "Assessment order"
        assert document["parse_status"] == "pending"
        assert document["size_bytes"] == len(b"%PDF-1.4 fake")
        assert document["uploaded_by_id"] == company_admin.id

    def test_the_bytes_land_under_a_generated_name(
        self, app_client, db, company, company_admin, upload_root
    ):
        upload(app_client, company_admin, filename="notice.pdf")

        document = db.query(Document).one()
        stored = upload_root / str(company.id)
        written = list(stored.iterdir())
        assert len(written) == 1
        # The generated name, not the uploaded one — and it keeps the suffix so
        # the extractor can still dispatch on it.
        assert written[0].name != "notice.pdf"
        assert written[0].suffix == ".pdf"
        assert document.storage_path == str(written[0])

    def test_a_traversing_filename_cannot_escape_the_directory(
        self, app_client, db, company, company_admin, upload_root
    ):
        response = upload(app_client, company_admin, filename="../../etc/passwd")

        assert response.status_code == 201
        # Nothing was written outside the org's own directory.
        assert [p.name for p in upload_root.iterdir()] == [str(company.id)]
        # And the echoed-back name has had its separators removed.
        assert "/" not in response.json()["document"]["original_filename"]

    def test_the_display_name_is_stripped_of_control_characters(
        self, app_client, company_admin
    ):
        response = upload(app_client, company_admin, filename="not<script>ice.pdf")

        assert response.json()["document"]["original_filename"] == "not_script_ice.pdf"

    def test_each_organization_gets_its_own_directory(
        self, app_client, db, company, other_company, company_admin, other_admin, upload_root
    ):
        upload(app_client, company_admin, content=b"one")
        upload(app_client, other_admin, content=b"two")

        assert {p.name for p in upload_root.iterdir()} == {
            str(company.id),
            str(other_company.id),
        }

    def test_the_same_bytes_twice_returns_the_first_document(
        self, app_client, db, company, company_admin, upload_root
    ):
        first = upload(app_client, company_admin, content=b"identical").json()
        second = upload(app_client, company_admin, content=b"identical").json()

        assert second["deduplicated"] is True
        assert second["parse_queued"] is False
        assert second["document"]["id"] == first["document"]["id"]
        assert db.query(Document).count() == 1
        # And the duplicate upload left nothing on disk to orphan.
        assert len(list((upload_root / str(company.id)).iterdir())) == 1

    def test_deduplication_does_not_reach_across_tenants(
        self, app_client, db, company_admin, other_admin
    ):
        upload(app_client, company_admin, content=b"identical")
        second = upload(app_client, other_admin, content=b"identical").json()

        assert second["deduplicated"] is False
        assert db.query(Document).count() == 2

    def test_an_unreadable_mime_type_is_refused(self, app_client, db, company_admin):
        response = upload(app_client, company_admin, mime="application/x-tar")

        assert response.status_code == 422
        body = response.json()["error"]
        assert "not a document type this parser can read" in body["message"]
        assert PDF_MIME in body["details"]["allowed"]
        assert db.query(Document).count() == 0

    def test_an_empty_file_is_refused(self, app_client, db, company_admin, upload_root):
        response = upload(app_client, company_admin, content=b"")

        assert response.status_code == 422
        assert "empty" in response.json()["error"]["message"]
        assert db.query(Document).count() == 0

    def test_a_file_past_the_limit_is_refused_and_leaves_nothing_behind(
        self, app_client, db, company, company_admin, monkeypatch, upload_root
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "max_upload_mb", 1)

        response = upload(app_client, company_admin, content=b"x" * (2 * 1024 * 1024))

        assert response.status_code == 422
        assert "1 MB limit" in response.json()["error"]["message"]
        assert db.query(Document).count() == 0
        # The partial write is cleaned up: a stray file is one the parse worker
        # would find with no row pointing at it.
        directory = upload_root / str(company.id)
        assert not directory.exists() or list(directory.iterdir()) == []

    def test_the_title_falls_back_to_the_filename(self, app_client, company_admin):
        body = upload(app_client, company_admin, filename="order.pdf").json()

        assert body["document"]["title"] == "order.pdf"

    def test_a_document_may_be_linked_to_a_filing(
        self, app_client, db, company, company_admin
    ):
        filing = make_filing(db, company, make_obligation(db))

        body = upload(app_client, company_admin, filing_id=filing.id).json()

        assert body["document"]["filing_id"] == filing.id

    def test_linking_to_another_tenants_filing_is_a_404(
        self, app_client, db, other_company, company_admin
    ):
        theirs = make_filing(db, other_company, make_obligation(db))

        response = upload(app_client, company_admin, filing_id=theirs.id)

        assert response.status_code == 404

    def test_the_upload_is_audited(self, app_client, db, company, company_admin):
        from app.models.audit import AuditTrail

        upload(app_client, company_admin, title="Show-cause notice")

        entry = db.query(AuditTrail).filter_by(entity_type="document").one()
        assert str(entry.action) == "create"
        assert entry.after_json["title"] == "Show-cause notice"
        assert entry.after_json["content_hash"]

    def test_a_read_only_user_may_not_upload(self, app_client, company_reader):
        assert upload(app_client, company_reader).status_code == 403

    def test_staff_may_upload(self, app_client, company_staff):
        assert upload(app_client, company_staff).status_code == 201

    def test_it_needs_a_token(self, app_client):
        response = app_client.post(
            f"{API}/documents", files={"file": ("a.pdf", io.BytesIO(b"x"), PDF_MIME)}
        )

        assert response.status_code == 401


class TestListing:
    def test_a_tenant_sees_only_their_own(
        self, app_client, db, company, other_company, company_admin
    ):
        make_document(db, company)
        make_document(db, other_company)

        assert app_client.get(f"{API}/documents", headers=auth(company_admin)).json()[
            "total"
        ] == 1

    def test_a_soft_deleted_document_is_hidden(
        self, app_client, db, company, company_admin
    ):
        document = make_document(db, company)
        document.soft_delete()
        db.flush()

        assert app_client.get(f"{API}/documents", headers=auth(company_admin)).json()[
            "total"
        ] == 0

    def test_the_summary_omits_the_parsed_body(
        self, app_client, db, company, company_admin
    ):
        make_document(db, company, parsed_content="forty pages of assessment order")

        item = app_client.get(f"{API}/documents", headers=auth(company_admin)).json()[
            "items"
        ][0]

        assert "parsed_content" not in item

    def test_filtering_by_type(self, app_client, db, company, company_admin):
        make_document(db, company, type=DocumentType.CIRCULAR)
        make_document(db, company, type=DocumentType.SHOW_CAUSE_NOTICE)

        response = app_client.get(
            f"{API}/documents", headers=auth(company_admin), params={"type": "circular"}
        )

        assert response.json()["total"] == 1

    def test_filtering_by_regulation_and_parse_status(
        self, app_client, db, company, company_admin
    ):
        make_document(db, company, regulation=Regulation.GST)
        make_document(
            db, company, regulation=Regulation.MCA, parse_status=ParseStatus.PARSED
        )

        by_regulation = app_client.get(
            f"{API}/documents", headers=auth(company_admin), params={"regulation": "gst"}
        )
        by_status = app_client.get(
            f"{API}/documents",
            headers=auth(company_admin),
            params={"parse_status": "parsed"},
        )

        assert by_regulation.json()["total"] == 1
        assert by_status.json()["items"][0]["regulation"] == "mca"

    def test_filtering_by_filing(self, app_client, db, company, company_admin):
        filing = make_filing(db, company, make_obligation(db))
        make_document(db, company, filing_id=filing.id)
        make_document(db, company)

        response = app_client.get(
            f"{API}/documents",
            headers=auth(company_admin),
            params={"filing_id": filing.id},
        )

        assert response.json()["total"] == 1

    def test_filtering_on_whether_a_deadline_was_found(
        self, app_client, db, company, company_admin
    ):
        make_document(db, company, extracted_deadline=date(2026, 9, 1))
        make_document(db, company)

        with_deadline = app_client.get(
            f"{API}/documents",
            headers=auth(company_admin),
            params={"has_deadline": True},
        )
        without = app_client.get(
            f"{API}/documents",
            headers=auth(company_admin),
            params={"has_deadline": False},
        )

        assert with_deadline.json()["total"] == 1
        assert without.json()["total"] == 1

    def test_search_covers_the_title_the_filename_and_the_summary(
        self, app_client, db, company, company_admin
    ):
        make_document(db, company, title="Assessment order for FY2024-25")
        make_document(db, company, title="Other", original_filename="gst_notice.pdf")
        make_document(
            db, company, title="Third", extracted_summary="A demand under section 73"
        )

        def search(term):
            return app_client.get(
                f"{API}/documents", headers=auth(company_admin), params={"search": term}
            ).json()["total"]

        assert search("assessment") == 1
        assert search("gst_notice") == 1
        assert search("section 73") == 1
        assert search("nothing here") == 0

    def test_the_newest_document_is_first(self, app_client, db, company, company_admin):
        older = make_document(db, company, title="Older")
        newer = make_document(db, company, title="Newer")

        titles = [
            i["title"]
            for i in app_client.get(
                f"{API}/documents", headers=auth(company_admin)
            ).json()["items"]
        ]

        assert titles.index(newer.title) < titles.index(older.title)

    def test_pagination_reports_the_full_total(
        self, app_client, db, company, company_admin
    ):
        for _ in range(5):
            make_document(db, company)

        body = app_client.get(
            f"{API}/documents", headers=auth(company_admin), params={"limit": 2}
        ).json()

        assert len(body["items"]) == 2
        assert body["total"] == 5


class TestRetrieval:
    def test_the_detail_carries_the_parsed_body(
        self, app_client, db, company, company_admin
    ):
        document = make_document(
            db,
            company,
            parsed_content="the full text",
            extracted_json={"summary": "a demand"},
        )

        body = app_client.get(
            f"{API}/documents/{document.id}", headers=auth(company_admin)
        ).json()

        assert body["parsed_content"] == "the full text"
        assert body["extracted_json"] == {"summary": "a demand"}

    def test_another_tenants_document_is_a_404(
        self, app_client, db, other_company, company_admin
    ):
        theirs = make_document(db, other_company)

        response = app_client.get(
            f"{API}/documents/{theirs.id}", headers=auth(company_admin)
        )

        assert response.status_code == 404

    def test_a_missing_document_is_a_404(self, app_client, company_admin):
        assert (
            app_client.get(f"{API}/documents/9999", headers=auth(company_admin)).status_code
            == 404
        )


class TestDownload:
    def test_it_streams_the_stored_bytes_back(
        self, app_client, db, company, company_admin
    ):
        created = upload(app_client, company_admin, content=b"%PDF-1.4 real bytes")
        document_id = created.json()["document"]["id"]

        response = app_client.get(
            f"{API}/documents/{document_id}/download", headers=auth(company_admin)
        )

        assert response.status_code == 200
        assert response.content == b"%PDF-1.4 real bytes"

    def test_a_download_is_recorded_in_the_audit_chain(
        self, app_client, db, company, company_admin
    ):
        from app.models.audit import AuditTrail

        document_id = upload(app_client, company_admin).json()["document"]["id"]

        app_client.get(
            f"{API}/documents/{document_id}/download", headers=auth(company_admin)
        )

        reads = (
            db.query(AuditTrail)
            .filter_by(entity_type="document", action="read")
            .all()
        )
        assert len(reads) == 1
        assert reads[0].entity_id == str(document_id)

    def test_a_row_whose_file_vanished_says_so(
        self, app_client, db, company, company_admin
    ):
        document = make_document(db, company, storage_path="/nonexistent/gone.pdf")

        response = app_client.get(
            f"{API}/documents/{document.id}/download", headers=auth(company_admin)
        )

        assert response.status_code == 404
        # A distinct message from "no such document", so a missing volume is
        # diagnosable rather than looking like a permissions problem.
        assert "stored file" in response.json()["error"]["message"]

    def test_another_tenant_may_not_download(
        self, app_client, db, other_company, company_admin
    ):
        theirs = make_document(db, other_company)

        response = app_client.get(
            f"{API}/documents/{theirs.id}/download", headers=auth(company_admin)
        )

        assert response.status_code == 404


class TestUpdate:
    def test_metadata_can_be_corrected(self, app_client, db, company, company_admin):
        document = make_document(db, company, title="Untitled document")

        body = app_client.patch(
            f"{API}/documents/{document.id}",
            headers=auth(company_admin),
            json={"title": "Assessment order FY2024-25", "regulation": "income_tax"},
        ).json()

        assert body["title"] == "Assessment order FY2024-25"
        assert body["regulation"] == "income_tax"

    def test_a_wrong_extracted_deadline_can_be_overridden(
        self, app_client, db, company, company_admin
    ):
        document = make_document(db, company, extracted_deadline=date(2026, 1, 1))

        body = app_client.patch(
            f"{API}/documents/{document.id}",
            headers=auth(company_admin),
            json={"extracted_deadline": "2026-09-15"},
        ).json()

        assert body["extracted_deadline"] == "2026-09-15"

    def test_only_the_changed_fields_are_audited(
        self, app_client, db, company, company_admin
    ):
        from app.models.audit import AuditTrail

        document = make_document(db, company, title="Before")

        app_client.patch(
            f"{API}/documents/{document.id}",
            headers=auth(company_admin),
            json={"title": "After", "type": str(document.type)},
        )

        entry = db.query(AuditTrail).filter_by(entity_type="document").one()
        assert entry.before_json == {"title": "Before"}
        assert entry.after_json == {"title": "After"}

    def test_a_no_op_patch_writes_no_audit_entry(
        self, app_client, db, company, company_admin
    ):
        from app.models.audit import AuditTrail

        document = make_document(db, company, title="Unchanged")

        response = app_client.patch(
            f"{API}/documents/{document.id}",
            headers=auth(company_admin),
            json={"title": "Unchanged"},
        )

        assert response.status_code == 200
        assert db.query(AuditTrail).filter_by(entity_type="document").count() == 0

    def test_relinking_to_another_tenants_filing_is_a_404(
        self, app_client, db, company, other_company, company_admin
    ):
        document = make_document(db, company)
        theirs = make_filing(db, other_company, make_obligation(db))

        response = app_client.patch(
            f"{API}/documents/{document.id}",
            headers=auth(company_admin),
            json={"filing_id": theirs.id},
        )

        assert response.status_code == 404

    def test_a_read_only_user_may_not_patch(
        self, app_client, db, company, company_reader
    ):
        document = make_document(db, company)

        response = app_client.patch(
            f"{API}/documents/{document.id}",
            headers=auth(company_reader),
            json={"title": "Nope"},
        )

        assert response.status_code == 403

    def test_an_empty_title_is_refused(self, app_client, db, company, company_admin):
        document = make_document(db, company)

        response = app_client.patch(
            f"{API}/documents/{document.id}",
            headers=auth(company_admin),
            json={"title": ""},
        )

        assert response.status_code == 422


class TestDelete:
    def test_deleting_keeps_the_bytes(
        self, app_client, db, company, company_admin, upload_root
    ):
        created = upload(app_client, company_admin).json()["document"]
        stored = list((upload_root / str(company.id)).iterdir())

        response = app_client.delete(
            f"{API}/documents/{created['id']}", headers=auth(company_admin)
        )

        assert response.status_code == 200
        assert response.json()["detail"] == {"file_retained": True}
        # The row is gone from the API; the evidence is not gone from disk.
        assert stored[0].is_file()
        assert (
            app_client.get(
                f"{API}/documents/{created['id']}", headers=auth(company_admin)
            ).status_code
            == 404
        )

    def test_the_deletion_is_audited_with_the_hash(
        self, app_client, db, company, company_admin
    ):
        from app.models.audit import AuditTrail

        document = make_document(db, company, content_hash="abc123", title="Notice")

        app_client.delete(f"{API}/documents/{document.id}", headers=auth(company_admin))

        entry = (
            db.query(AuditTrail)
            .filter_by(entity_type="document", action="soft_delete")
            .one()
        )
        assert entry.before_json == {"title": "Notice", "content_hash": "abc123"}

    def test_staff_may_not_delete(self, app_client, db, company, company_staff):
        document = make_document(db, company)

        response = app_client.delete(
            f"{API}/documents/{document.id}", headers=auth(company_staff)
        )

        assert response.status_code == 403

    def test_a_compliance_manager_may_delete(self, app_client, db, company):
        manager = make_user(db, company, role=UserRole.COMPLIANCE_MANAGER)
        document = make_document(db, company)

        response = app_client.delete(
            f"{API}/documents/{document.id}", headers=auth(manager)
        )

        assert response.status_code == 200

    def test_another_tenants_document_cannot_be_deleted(
        self, app_client, db, other_company, company_admin
    ):
        theirs = make_document(db, other_company)

        response = app_client.delete(
            f"{API}/documents/{theirs.id}", headers=auth(company_admin)
        )

        assert response.status_code == 404


class TestExtractedDeadlines:
    def test_it_returns_only_documents_carrying_a_deadline(
        self, app_client, db, company, company_admin
    ):
        make_document(db, company, extracted_deadline=date(2026, 9, 1))
        make_document(db, company)

        rows = app_client.get(
            f"{API}/documents/deadlines/extracted", headers=auth(company_admin)
        ).json()

        assert len(rows) == 1

    def test_they_come_back_in_date_order(self, app_client, db, company, company_admin):
        for day in (20, 5, 12):
            make_document(db, company, extracted_deadline=date(2026, 9, day))

        rows = app_client.get(
            f"{API}/documents/deadlines/extracted", headers=auth(company_admin)
        ).json()

        assert [r["extracted_deadline"] for r in rows] == [
            "2026-09-05",
            "2026-09-12",
            "2026-09-20",
        ]

    def test_the_range_can_be_bounded_at_both_ends(
        self, app_client, db, company, company_admin
    ):
        for day in (1, 15, 28):
            make_document(db, company, extracted_deadline=date(2026, 9, day))

        rows = app_client.get(
            f"{API}/documents/deadlines/extracted",
            headers=auth(company_admin),
            params={"from_date": "2026-09-10", "to_date": "2026-09-20"},
        ).json()

        assert [r["extracted_deadline"] for r in rows] == ["2026-09-15"]

    def test_another_tenants_deadlines_are_invisible(
        self, app_client, db, other_company, company_admin
    ):
        make_document(db, other_company, extracted_deadline=date(2026, 9, 1))

        rows = app_client.get(
            f"{API}/documents/deadlines/extracted", headers=auth(company_admin)
        ).json()

        assert rows == []


def _write(upload_root, org_id, name: str, content: bytes) -> str:
    directory = upload_root / str(org_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    return str(path)


class TestParsePipeline:
    def test_a_readable_file_is_parsed_and_the_method_recorded(
        self, db, company, upload_root
    ):
        path = _write(upload_root, company.id, "notice.txt", b"A demand under s.73.")
        document = make_document(db, company, storage_path=path, mime_type="text/plain")

        outcome = document_tasks.parse_document(db, document)

        assert outcome["status"] == "parsed"
        assert document.parse_status == ParseStatus.PARSED
        assert "s.73" in document.parsed_content
        assert document.extraction_method
        assert document.parsed_at is not None
        assert document.parse_error is None

    def test_an_unreadable_file_fails_with_the_reason_on_the_row(
        self, db, company, upload_root
    ):
        document = make_document(db, company, storage_path="/nonexistent/gone.pdf")

        outcome = document_tasks.parse_document(db, document)

        assert outcome == {
            "document_id": document.id,
            "status": "failed",
            "reason": "unreadable",
        }
        # FAILED, never left PENDING: the queue selects PENDING, so a document
        # stuck there would be retried forever.
        assert document.parse_status == ParseStatus.FAILED
        assert document.parse_error
        assert document.parsed_at is not None

    def test_a_scan_fails_with_a_reason_that_names_the_missing_capability(
        self, db, company, upload_root, monkeypatch
    ):
        path = _write(upload_root, company.id, "scan.pdf", b"%PDF-1.4")
        document = make_document(db, company, storage_path=path)

        def _scanned(*_args, **_kwargs):
            raise extraction.ScannedDocument("No text layer")

        monkeypatch.setattr(extraction, "extract_text", _scanned)

        outcome = document_tasks.parse_document(db, document)

        assert outcome["reason"] == "scanned"
        assert document.parse_status == ParseStatus.FAILED
        assert "OCR" in document.parse_error

    def test_without_a_key_the_text_is_kept_and_no_analysis_is_claimed(
        self, db, company, upload_root, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "openrouter_api_key", "")
        path = _write(upload_root, company.id, "notice.txt", b"Plain text notice.")
        document = make_document(db, company, storage_path=path, mime_type="text/plain")

        outcome = document_tasks.parse_document(db, document)

        assert outcome["analysed"] is False
        assert document.parse_status == ParseStatus.PARSED
        assert document.parsed_content
        assert document.extracted_json is None

    def test_an_analysis_result_is_stored_with_its_provenance(
        self, db, company, upload_root, monkeypatch
    ):
        path = _write(upload_root, company.id, "notice.txt", b"Reply within 15 days.")
        document = make_document(db, company, storage_path=path, mime_type="text/plain")

        monkeypatch.setattr(llm, "is_configured", lambda: True)
        monkeypatch.setattr(
            llm,
            "complete_json",
            lambda *_a, **_k: llm.LLMResult(
                content="{}",
                model="test-model",
                prompt_tokens=10,
                completion_tokens=5,
                data={
                    "summary": "A show-cause notice requiring a reply.",
                    "deadline": "2026-09-15",
                    "requirements": ["Reply in writing"],
                },
            ),
        )

        outcome = document_tasks.parse_document(db, document)

        assert outcome["analysed"] is True
        assert document.extracted_summary == "A show-cause notice requiring a reply."
        assert document.extracted_deadline == date(2026, 9, 15)
        # Provenance travels with the content so an export says which model
        # produced it.
        assert document.extracted_json["_model"] == "test-model"
        assert document.extracted_json["_tokens"] == 15

    def test_an_upstream_failure_keeps_the_text_and_says_analysis_is_missing(
        self, db, company, upload_root, monkeypatch
    ):
        path = _write(upload_root, company.id, "notice.txt", b"Some text.")
        document = make_document(db, company, storage_path=path, mime_type="text/plain")

        monkeypatch.setattr(llm, "is_configured", lambda: True)

        def _down(*_a, **_k):
            raise llm.UpstreamError("502 from the provider")

        monkeypatch.setattr(llm, "complete_json", _down)

        outcome = document_tasks.parse_document(db, document)

        # The mechanical step worked, so the document is PARSED — with the
        # analysis failure recorded, so a retry need not re-read the file.
        assert outcome == {
            "document_id": document.id,
            "status": "parsed",
            "analysed": False,
        }
        assert document.parse_status == ParseStatus.PARSED
        assert document.parsed_content == "Some text."
        assert "analysis unavailable" in document.parse_error

    def test_a_relative_upload_root_is_not_double_prefixed(
        self, db, company, monkeypatch, tmp_path
    ):
        """``UPLOAD_DIR``'s documented default, ``"data/documents"``, is
        relative — every other test in this class points it at an absolute
        ``upload_root`` instead, which is what let a double-prefixing bug
        here go unnoticed for a relative root.

        ``storage_path`` is written by :func:`app.routers.documents._store`
        as ``_storage_root() / org_id / filename`` — it already carries
        whatever the root resolved to, exactly as
        :func:`app.routers.documents.download_document` reads it back with
        no reconstruction. A parser that re-joined the root onto that value
        would look for the file twice as deep and never find it.
        """
        from app.core.config import settings

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(settings, "upload_dir", "data/documents")

        directory = tmp_path / "data" / "documents" / str(company.id)
        directory.mkdir(parents=True)
        (directory / "relative.txt").write_bytes(b"Found via the relative root.")

        document = make_document(
            db,
            company,
            storage_path=f"data/documents/{company.id}/relative.txt",
            mime_type="text/plain",
        )

        outcome = document_tasks.parse_document(db, document)

        assert outcome["status"] == "parsed"
        assert document.parsed_content == "Found via the relative root."


class TestDeadlineParsing:
    @pytest.mark.parametrize(
        "value",
        [None, "", "next Tuesday", "15/09/2026", "September 15", 20260915, {"d": 1}],
    )
    def test_anything_not_an_iso_date_is_dropped_rather_than_guessed(self, value):
        assert document_tasks._parse_date(value) is None

    def test_an_iso_date_is_read(self):
        assert document_tasks._parse_date("2026-09-15") == date(2026, 9, 15)

    def test_a_timestamp_is_truncated_to_its_date(self):
        assert document_tasks._parse_date("2026-09-15T10:30:00Z") == date(2026, 9, 15)

    def test_surrounding_whitespace_is_tolerated(self):
        assert document_tasks._parse_date("  2026-09-15  ") == date(2026, 9, 15)
