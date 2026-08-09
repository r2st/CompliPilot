"""Reading the audit trail over HTTP: who may, what they see, and the export.

The chain's arithmetic is tested in ``test_audit_chain.py``. This file is about
the four questions the router answers on top of it, none of which the service
layer can get right on its own.

**Who may read it.** The trail names every actor, their address and the payload
they sent. That is a more sensitive read than the records it describes — the
person most interested in it is a departing employee — so the list is Compliance
Manager and above, and the two endpoints that answer "is the chain intact" and
"give me the whole thing as a file" are Admin only.

**Whose trail they see.** Every query is filtered to the organization in
context, and for a CA firm acting for a client that is the *client's* chain, not
the firm's. Both halves matter: a firm reviewing a client's history must see it,
and must not see the neighbouring client's.

**Which way round it reads.** The list is newest first, because a reviewer
starts from what just happened; one entity's history is oldest first, because
that is read as a narrative. Those are opposite orderings by intent, and an
"obvious" cleanup that unified them would break one of the two readings.

**What the export is.** A file handed to an outside auditor, carrying the
checksum columns so the recipient can verify the chain without trusting us —
and itself an event worth recording, since "who took a full copy of the trail"
is precisely the question the trail exists to answer.
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta

import pytest

from app.models.audit import GENESIS_CHECKSUM, AuditTrail
from app.models.enums import AuditAction, UserRole
from app.models.mixins import utcnow
from app.services import audit as audit_service
from tests.conftest import API, auth, make_engagement, make_user


def entry(db, org, **kwargs):
    """One entry, written the only way entries are ever written.

    Through :func:`app.services.audit.record` rather than by constructing an
    ``AuditTrail``, so the sequence, the links and the checksum are the real
    ones — the export and the verify endpoint both return them, and a fixture
    that invented them would let those tests pass on values the application
    could never produce.
    """
    kwargs.setdefault("action", AuditAction.UPDATE)
    kwargs.setdefault("entity_type", "filing")
    kwargs.setdefault("entity_id", 1)
    kwargs.setdefault("summary", "Filing marked ready for review")
    return audit_service.record(db, organization_id=org.id, **kwargs)


def backdate(db, row, days: int):
    """Move an entry *days* into the past.

    This invalidates its checksum, which is the point of the chain and not a
    problem here: the tests that use it are asking which rows a date window
    selects, and none of them also verify. Anything that does verify builds its
    chain forwards and leaves the timestamps alone.
    """
    row.timestamp = utcnow() - timedelta(days=days)
    db.flush()
    return row


def manager(db, org):
    return make_user(db, org, role=UserRole.COMPLIANCE_MANAGER, full_name="Devika Nair")


# --------------------------------------------------------------------------
# Who may read it
# --------------------------------------------------------------------------


class TestListAccess:
    def test_a_compliance_manager_may_read_the_trail(self, app_client, db, company):
        entry(db, company)

        response = app_client.get(f"{API}/audit", headers=auth(manager(db, company)))

        assert response.status_code == 200
        assert response.json()["total"] == 1

    def test_an_admin_may_read_the_trail(self, app_client, db, company, company_admin):
        entry(db, company)

        assert app_client.get(f"{API}/audit", headers=auth(company_admin)).status_code == 200

    def test_staff_may_not(self, app_client, db, company, company_staff):
        """A Staff account files returns. It does not review who else did.

        The trail carries addresses and payloads for the whole organization, so
        widening this to everyone who can write is a disclosure, not a
        convenience.
        """
        entry(db, company)

        response = app_client.get(f"{API}/audit", headers=auth(company_staff))

        assert response.status_code == 403
        assert "compliance_manager" in response.text

    def test_a_read_only_account_may_not(self, app_client, company_reader):
        assert app_client.get(f"{API}/audit", headers=auth(company_reader)).status_code == 403

    def test_an_unauthenticated_request_is_refused(self, app_client, db, company):
        entry(db, company)

        assert app_client.get(f"{API}/audit").status_code == 401

    @pytest.mark.parametrize(
        "path", ["/audit", "/audit/head", "/audit/entity/filing/1"]
    )
    def test_every_manager_endpoint_is_closed_to_staff(
        self, app_client, company_staff, path
    ):
        assert app_client.get(f"{API}{path}", headers=auth(company_staff)).status_code == 403

    @pytest.mark.parametrize("path", ["/audit/verify", "/audit/export"])
    def test_the_admin_endpoints_are_closed_to_a_manager(
        self, app_client, db, company, path
    ):
        """Verify and export are a step above reading.

        "Has our trail been tampered with" is an incident question, and a full
        CSV of four years of history is a data extract. Both belong to the
        person accountable for the answer.
        """
        response = app_client.get(f"{API}{path}", headers=auth(manager(db, company)))

        assert response.status_code == 403
        assert "admin" in response.text


class TestTenantIsolation:
    def test_another_tenants_entries_are_not_listed(
        self, app_client, db, company_admin, other_company
    ):
        entry(db, other_company, summary="Rival Industries filed GSTR-3B")

        body = app_client.get(f"{API}/audit", headers=auth(company_admin)).json()

        assert body["total"] == 0

    def test_a_firm_acting_for_a_client_reads_the_clients_chain(
        self, app_client, db, ca_firm, firm_admin, company, engagement
    ):
        """Not the firm's own.

        The point of the delegated token is that the firm is looking at the
        client's compliance position; a trail that answered with the firm's
        internal history instead would be both wrong and a leak in the other
        direction.
        """
        entry(db, company, summary="Client filing approved")
        entry(db, ca_firm, summary="Firm's own internal change")

        body = app_client.get(
            f"{API}/audit", headers=auth(firm_admin, client_org_id=company.id)
        ).json()

        assert [e["summary"] for e in body["items"]] == ["Client filing approved"]

    def test_a_firm_cannot_read_a_client_it_does_not_act_for(
        self, app_client, db, firm_admin, other_company
    ):
        entry(db, other_company)

        response = app_client.get(
            f"{API}/audit", headers=auth(firm_admin, client_org_id=other_company.id)
        )

        assert response.status_code == 403

    def test_a_delegated_action_is_visible_from_both_sides(
        self, app_client, firm_admin, company, engagement
    ):
        """What :func:`audit_service.record_for` writes twice, this reads twice.

        The client's chain shows their filing changed; the firm's shows which
        of its people changed it. A regulator asking either question gets an
        answer from the organization it asked.
        """
        # The export is the one auditable action this router performs itself,
        # so it stands in for any delegated write: the assertion is about how
        # the pair of entries reads back, not about which action made them.
        app_client.get(
            f"{API}/audit/export", headers=auth(firm_admin, client_org_id=company.id)
        )

        client_side = app_client.get(
            f"{API}/audit", headers=auth(firm_admin, client_org_id=company.id)
        ).json()
        firm_side = app_client.get(f"{API}/audit", headers=auth(firm_admin)).json()

        assert client_side["items"][0]["summary"] == "Audit trail exported"
        assert "for client organization" in firm_side["items"][0]["summary"]
        assert firm_side["items"][0]["user_id"] == firm_admin.id


# --------------------------------------------------------------------------
# The list
# --------------------------------------------------------------------------


class TestListOrderingAndPaging:
    def test_the_newest_entry_comes_first(self, app_client, db, company, company_admin):
        entry(db, company, summary="first")
        entry(db, company, summary="second")
        entry(db, company, summary="third")

        body = app_client.get(f"{API}/audit", headers=auth(company_admin)).json()

        assert [e["summary"] for e in body["items"]] == ["third", "second", "first"]

    def test_ordering_is_by_sequence_not_timestamp(
        self, app_client, db, company, company_admin
    ):
        """Two entries written in the same millisecond still have an order.

        ``utcnow()`` is not guaranteed to differ between two writes inside one
        request, and a trail that reordered itself under a coarse clock would
        make a reviewer read the approval before the submission.
        """
        first = entry(db, company, summary="first")
        second = entry(db, company, summary="second")
        second.timestamp = first.timestamp
        db.flush()

        body = app_client.get(f"{API}/audit", headers=auth(company_admin)).json()

        assert [e["summary"] for e in body["items"]] == ["second", "first"]

    def test_the_page_reports_the_total_not_the_page_size(
        self, app_client, db, company, company_admin
    ):
        for n in range(5):
            entry(db, company, summary=f"change {n}")

        body = app_client.get(
            f"{API}/audit", headers=auth(company_admin), params={"limit": 2}
        ).json()

        assert len(body["items"]) == 2
        assert body["total"] == 5

    def test_the_offset_walks_backwards_through_history(
        self, app_client, db, company, company_admin
    ):
        for n in range(5):
            entry(db, company, summary=f"change {n}")

        body = app_client.get(
            f"{API}/audit", headers=auth(company_admin), params={"limit": 2, "offset": 2}
        ).json()

        assert [e["summary"] for e in body["items"]] == ["change 2", "change 1"]

    def test_an_empty_trail_is_an_empty_page_not_an_error(
        self, app_client, company_admin
    ):
        body = app_client.get(f"{API}/audit", headers=auth(company_admin)).json()

        assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}

    def test_the_page_size_is_capped(self, app_client, company_admin):
        """An unbounded limit turns the list endpoint into the export endpoint.

        Which is Admin-only and audited, and this one is neither.
        """
        response = app_client.get(
            f"{API}/audit", headers=auth(company_admin), params={"limit": 5000}
        )

        assert response.status_code == 422


class TestListFilters:
    def test_filtering_by_entity_type(self, app_client, db, company, company_admin):
        entry(db, company, entity_type="filing")
        entry(db, company, entity_type="document")

        body = app_client.get(
            f"{API}/audit", headers=auth(company_admin), params={"entity_type": "document"}
        ).json()

        assert [e["entity_type"] for e in body["items"]] == ["document"]

    def test_filtering_by_entity_id(self, app_client, db, company, company_admin):
        entry(db, company, entity_id=11)
        entry(db, company, entity_id=22)

        body = app_client.get(
            f"{API}/audit", headers=auth(company_admin), params={"entity_id": "22"}
        ).json()

        assert [e["entity_id"] for e in body["items"]] == ["22"]

    def test_filtering_by_action(self, app_client, db, company, company_admin):
        entry(db, company, action=AuditAction.CREATE)
        entry(db, company, action=AuditAction.SOFT_DELETE)

        body = app_client.get(
            f"{API}/audit", headers=auth(company_admin), params={"action": "soft_delete"}
        ).json()

        assert [e["action"] for e in body["items"]] == ["soft_delete"]

    def test_an_action_outside_the_vocabulary_is_rejected(
        self, app_client, company_admin
    ):
        response = app_client.get(
            f"{API}/audit", headers=auth(company_admin), params={"action": "exfiltrate"}
        )

        assert response.status_code == 422

    def test_filtering_by_actor(self, app_client, db, company, company_admin):
        """The "what did this person do" question, which is why user_id is a
        filter and not something a reviewer scrolls for."""
        other = manager(db, company)
        entry(db, company, user_id=company_admin.id, summary="admin's change")
        entry(db, company, user_id=other.id, summary="manager's change")

        body = app_client.get(
            f"{API}/audit", headers=auth(company_admin), params={"user_id": other.id}
        ).json()

        assert [e["summary"] for e in body["items"]] == ["manager's change"]

    def test_filters_combine(self, app_client, db, company, company_admin):
        entry(db, company, entity_type="filing", action=AuditAction.CREATE)
        entry(db, company, entity_type="filing", action=AuditAction.SUBMIT)
        entry(db, company, entity_type="document", action=AuditAction.SUBMIT)

        body = app_client.get(
            f"{API}/audit",
            headers=auth(company_admin),
            params={"entity_type": "filing", "action": "submit"},
        ).json()

        assert body["total"] == 1

    def test_the_start_date_excludes_what_came_before_it(
        self, app_client, db, company, company_admin
    ):
        backdate(db, entry(db, company, summary="last week"), days=7)
        entry(db, company, summary="today")

        body = app_client.get(
            f"{API}/audit",
            headers=auth(company_admin),
            params={"start_date": date.today().isoformat()},
        ).json()

        assert [e["summary"] for e in body["items"]] == ["today"]

    def test_the_end_date_includes_the_whole_of_that_day(
        self, app_client, db, company, company_admin
    ):
        """"To the 14th" means through the end of the 14th.

        Comparing a timestamp against the bare date would silently drop
        everything that happened on the last day of the window — the day a
        reviewer investigating an incident cares most about.
        """
        entry(db, company, summary="this afternoon")

        body = app_client.get(
            f"{API}/audit",
            headers=auth(company_admin),
            params={"end_date": date.today().isoformat()},
        ).json()

        assert [e["summary"] for e in body["items"]] == ["this afternoon"]

    def test_a_window_selects_only_what_falls_inside_it(
        self, app_client, db, company, company_admin
    ):
        backdate(db, entry(db, company, summary="too old"), days=10)
        backdate(db, entry(db, company, summary="inside"), days=3)
        entry(db, company, summary="too new")
        today = date.today()

        body = app_client.get(
            f"{API}/audit",
            headers=auth(company_admin),
            params={
                "start_date": (today - timedelta(days=5)).isoformat(),
                "end_date": (today - timedelta(days=1)).isoformat(),
            },
        ).json()

        assert [e["summary"] for e in body["items"]] == ["inside"]


class TestEntryContent:
    def test_the_checksums_are_returned(self, app_client, db, company, company_admin):
        """Not hidden as an implementation detail.

        A client holding an exported head checksum can check this response
        against it without trusting ``/audit/verify`` — which is the check that
        still works when the API itself is the thing that has been compromised.
        """
        written = entry(db, company)

        (item,) = app_client.get(f"{API}/audit", headers=auth(company_admin)).json()["items"]

        assert item["checksum"] == written.checksum
        assert item["prev_checksum"] == GENESIS_CHECKSUM

    def test_the_before_and_after_payload_is_returned(
        self, app_client, db, company, company_admin
    ):
        entry(
            db,
            company,
            before={"status": "draft"},
            after={"status": "submitted"},
        )

        (item,) = app_client.get(f"{API}/audit", headers=auth(company_admin)).json()["items"]

        assert item["before_json"] == {"status": "draft"}
        assert item["after_json"] == {"status": "submitted"}

    def test_a_redacted_payload_stays_redacted_over_the_wire(
        self, app_client, db, company, company_admin
    ):
        """The redaction happens on write, so this is really asserting that no
        read path un-redacts. It is worth asserting anyway: the trail is the
        one table read by more people than the data it describes."""
        entry(db, company, after={"email": "x@example.com", "password": "hunter2"})

        (item,) = app_client.get(f"{API}/audit", headers=auth(company_admin)).json()["items"]

        assert item["after_json"]["password"] == "[redacted]"
        assert item["after_json"]["email"] == "x@example.com"

    def test_a_system_entry_names_the_system_as_the_actor(
        self, app_client, db, company, company_admin
    ):
        """A scheduled sweep has no user, and the trail still has to say who.

        ``user_id`` null with an empty actor would leave an entry nobody can
        attribute, which is the one thing an audit row must never be.
        """
        entry(db, company, user_id=None, actor_label="system")

        (item,) = app_client.get(f"{API}/audit", headers=auth(company_admin)).json()["items"]

        assert item["user_id"] is None
        assert item["actor_label"] == "system"


# --------------------------------------------------------------------------
# One record's history
# --------------------------------------------------------------------------


class TestEntityHistory:
    def test_the_history_reads_oldest_first(self, app_client, db, company, company_admin):
        """Opposite to the main list, and deliberately so.

        This is read as a narrative — drafted, reviewed, filed — and a story
        told backwards is harder to follow than one told forwards.
        """
        for summary in ("drafted", "reviewed", "filed"):
            entry(db, company, entity_type="filing", entity_id=9, summary=summary)

        body = app_client.get(
            f"{API}/audit/entity/filing/9", headers=auth(company_admin)
        ).json()

        assert [e["summary"] for e in body] == ["drafted", "reviewed", "filed"]

    def test_only_that_record_is_returned(self, app_client, db, company, company_admin):
        entry(db, company, entity_type="filing", entity_id=9, summary="mine")
        entry(db, company, entity_type="filing", entity_id=10, summary="a sibling")
        entry(db, company, entity_type="document", entity_id=9, summary="same id, other type")

        body = app_client.get(
            f"{API}/audit/entity/filing/9", headers=auth(company_admin)
        ).json()

        assert [e["summary"] for e in body] == ["mine"]

    def test_a_record_with_no_history_is_an_empty_list(
        self, app_client, company_admin
    ):
        """Not a 404. The entity may exist and simply have had nothing done to
        it, and this endpoint cannot tell the difference — answering 404 would
        assert something about the entity that the trail does not know."""
        response = app_client.get(
            f"{API}/audit/entity/filing/404", headers=auth(company_admin)
        )

        assert response.status_code == 200
        assert response.json() == []

    def test_another_tenants_record_shows_nothing(
        self, app_client, db, company_admin, other_company
    ):
        entry(db, other_company, entity_type="filing", entity_id=9)

        body = app_client.get(
            f"{API}/audit/entity/filing/9", headers=auth(company_admin)
        ).json()

        assert body == []

    def test_the_history_is_bounded(self, app_client, db, company, company_admin):
        for n in range(5):
            entry(db, company, entity_type="filing", entity_id=9, summary=f"step {n}")

        body = app_client.get(
            f"{API}/audit/entity/filing/9", headers=auth(company_admin), params={"limit": 3}
        ).json()

        assert [e["summary"] for e in body] == ["step 0", "step 1", "step 2"]


# --------------------------------------------------------------------------
# Verification and the head
# --------------------------------------------------------------------------


class TestVerify:
    def test_an_untouched_chain_verifies(self, app_client, db, company, company_admin):
        for n in range(3):
            entry(db, company, summary=f"change {n}")

        body = app_client.get(f"{API}/audit/verify", headers=auth(company_admin)).json()

        assert body["is_valid"] is True
        assert body["entries_checked"] == 3
        assert body["broken_at_sequence"] is None

    def test_an_empty_chain_verifies(self, app_client, company, company_admin):
        body = app_client.get(f"{API}/audit/verify", headers=auth(company_admin)).json()

        assert body == {
            "organization_id": company.id,
            "entries_checked": 0,
            "is_valid": True,
            "broken_at_sequence": None,
            "broken_entry_id": None,
            "reason": None,
            "head_checksum": None,
        }

    def test_an_edited_entry_is_reported_at_its_position(
        self, app_client, db, company, company_admin
    ):
        entry(db, company, summary="innocuous")
        tampered = entry(db, company, summary="₹40,00,000 write-off approved")
        entry(db, company, summary="later")
        tampered.summary = "₹4,000 write-off approved"
        db.flush()

        body = app_client.get(f"{API}/audit/verify", headers=auth(company_admin)).json()

        assert body["is_valid"] is False
        assert body["broken_at_sequence"] == 2
        assert body["broken_entry_id"] == tampered.id

    def test_a_failed_verification_is_still_a_200(
        self, app_client, db, company, company_admin
    ):
        """The request succeeded; the answer is bad news.

        A 500 here would put "our audit trail has been tampered with" in the
        same bucket as "the database is down", and monitoring would treat it as
        an outage to be restarted rather than an incident to be investigated.
        """
        tampered = entry(db, company)
        tampered.summary = "rewritten"
        db.flush()

        response = app_client.get(f"{API}/audit/verify", headers=auth(company_admin))

        assert response.status_code == 200
        assert response.json()["is_valid"] is False

    def test_a_break_is_logged_at_error_level(
        self, app_client, db, company, company_admin, caplog
    ):
        """Nobody polls ``/audit/verify``. The log line is what wakes somebody."""
        tampered = entry(db, company)
        tampered.summary = "rewritten"
        db.flush()

        with caplog.at_level("ERROR", logger="app.routers.audit"):
            app_client.get(f"{API}/audit/verify", headers=auth(company_admin))

        assert "verification FAILED" in caplog.text

    def test_one_tenants_tampering_does_not_fail_anothers_verification(
        self, app_client, db, company, company_admin, other_company, other_admin
    ):
        """Chains are per-organization, and so is the blast radius."""
        entry(db, company)
        tampered = entry(db, other_company)
        tampered.summary = "rewritten"
        db.flush()

        assert app_client.get(
            f"{API}/audit/verify", headers=auth(company_admin)
        ).json()["is_valid"]
        assert not app_client.get(
            f"{API}/audit/verify", headers=auth(other_admin)
        ).json()["is_valid"]


class TestHead:
    def test_the_head_reports_the_tip_of_the_chain(
        self, app_client, db, company, company_admin
    ):
        entry(db, company)
        last = entry(db, company)

        body = app_client.get(f"{API}/audit/head", headers=auth(company_admin)).json()

        assert body["sequence"] == 2
        assert body["entry_count"] == 2
        assert body["checksum"] == last.checksum

    def test_an_empty_chain_heads_at_the_genesis_value(
        self, app_client, company, company_admin
    ):
        """Rather than null.

        The head is meant to be published on a schedule. A null on the first
        week would either break the publisher or be recorded as "no trail",
        neither of which is what "nothing has happened yet" means.
        """
        body = app_client.get(f"{API}/audit/head", headers=auth(company_admin)).json()

        assert body == {
            "organization_id": company.id,
            "sequence": 0,
            "entry_count": 0,
            "checksum": GENESIS_CHECKSUM,
            "as_of": body["as_of"],
        }

    def test_the_head_matches_what_verification_reports(
        self, app_client, db, company, company_admin
    ):
        """The two endpoints must agree, or the published value proves nothing.

        This is the whole point of publishing the head: an auditor compares
        last month's published checksum against today's verification, and a
        mismatch between our own two endpoints would make that comparison
        meaningless.
        """
        for _ in range(3):
            entry(db, company)

        head = app_client.get(f"{API}/audit/head", headers=auth(company_admin)).json()
        verified = app_client.get(f"{API}/audit/verify", headers=auth(company_admin)).json()

        assert head["checksum"] == verified["head_checksum"]

    def test_a_manager_may_read_the_head(self, app_client, db, company):
        """Unlike verify. Publishing the head is routine hygiene, and the value
        discloses nothing — it is a hash of history the reader may already
        list."""
        entry(db, company)

        response = app_client.get(f"{API}/audit/head", headers=auth(manager(db, company)))

        assert response.status_code == 200

    def test_the_head_is_per_organization(
        self, app_client, db, company, company_admin, other_company, other_admin
    ):
        entry(db, company)
        entry(db, other_company)
        entry(db, other_company)

        mine = app_client.get(f"{API}/audit/head", headers=auth(company_admin)).json()
        theirs = app_client.get(f"{API}/audit/head", headers=auth(other_admin)).json()

        assert (mine["sequence"], theirs["sequence"]) == (1, 2)
        assert mine["checksum"] != theirs["checksum"]


# --------------------------------------------------------------------------
# The export
# --------------------------------------------------------------------------


def export(app_client, user, **params):
    """The export as parsed CSV: ``(header, rows)``."""
    response = app_client.get(f"{API}/audit/export", headers=auth(user), params=params)
    assert response.status_code == 200, response.text
    header, *rows = list(csv.reader(io.StringIO(response.text)))
    return header, rows


class TestExport:
    def test_the_response_is_a_csv_attachment(self, app_client, db, company, company_admin):
        entry(db, company)

        response = app_client.get(f"{API}/audit/export", headers=auth(company_admin))

        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]

    def test_the_filename_names_the_organization_and_the_day(
        self, app_client, db, company, company_admin
    ):
        """So a folder of exports from three clients over six months is still
        readable without opening any of them."""
        entry(db, company)

        response = app_client.get(f"{API}/audit/export", headers=auth(company_admin))

        disposition = response.headers["content-disposition"]
        assert f"audit-trail-org{company.id}-{date.today().isoformat()}.csv" in disposition

    def test_the_checksum_columns_are_included(self, app_client, db, company, company_admin):
        """The recipient must be able to verify the chain without us.

        An export that omitted them would be a list of claims about history
        rather than evidence of it.
        """
        written = entry(db, company)

        header, rows = export(app_client, company_admin)

        assert "checksum" in header and "prev_checksum" in header
        assert rows[0][header.index("checksum")] == written.checksum
        assert rows[0][header.index("prev_checksum")] == GENESIS_CHECKSUM

    def test_the_rows_come_out_oldest_first(self, app_client, db, company, company_admin):
        """A file is read top to bottom, so the chain in it runs forwards — and
        a recipient re-verifying it can walk the links in one pass."""
        for n in range(3):
            entry(db, company, summary=f"change {n}")

        header, rows = export(app_client, company_admin)
        summaries = [r[header.index("summary")] for r in rows]

        assert summaries[:3] == ["change 0", "change 1", "change 2"]

    def test_an_export_is_itself_recorded(self, app_client, db, company_admin):
        """"Who took a copy of the whole trail" is exactly the question the
        trail exists to answer."""
        app_client.get(f"{API}/audit/export", headers=auth(company_admin))

        db.expire_all()
        (recorded,) = (
            db.query(AuditTrail).filter(AuditTrail.entity_type == "audit_trail").all()
        )
        assert recorded.action == AuditAction.EXPORT
        assert recorded.user_id == company_admin.id

    def test_the_recorded_export_names_the_window(
        self, app_client, db, company_admin
    ):
        app_client.get(
            f"{API}/audit/export",
            headers=auth(company_admin),
            params={"start_date": "2026-04-01", "end_date": "2026-06-30"},
        )

        db.expire_all()
        (recorded,) = (
            db.query(AuditTrail).filter(AuditTrail.entity_type == "audit_trail").all()
        )
        assert recorded.summary == "Audit trail exported from 2026-04-01 to 2026-06-30"

    def test_the_export_entry_appears_in_the_next_export_not_its_own(
        self, app_client, db, company, company_admin
    ):
        """A file that contained the record of its own creation would have a
        last row whose checksum the recipient cannot yet have seen — and,
        worse, would make "how many exports have there been" unanswerable from
        any single file. The entry is written first and shows up next time.
        """
        entry(db, company)

        header, first = export(app_client, company_admin)
        _, second = export(app_client, company_admin)

        actions = header.index("action")
        assert [r[actions] for r in first] == ["update"]
        assert [r[actions] for r in second] == ["update", "export"]

    def test_the_export_is_scoped_to_the_tenant(
        self, app_client, db, company, company_admin, other_company
    ):
        entry(db, company, summary="mine")
        entry(db, other_company, summary="theirs")

        header, rows = export(app_client, company_admin)

        assert [r[header.index("summary")] for r in rows] == ["mine"]

    def test_the_date_window_bounds_the_file(self, app_client, db, company, company_admin):
        backdate(db, entry(db, company, summary="last month"), days=40)
        entry(db, company, summary="this month")

        header, rows = export(
            app_client,
            company_admin,
            start_date=(date.today() - timedelta(days=7)).isoformat(),
        )

        assert [r[header.index("summary")] for r in rows] == ["this month"]

    def test_the_entity_type_filter_bounds_the_file(
        self, app_client, db, company, company_admin
    ):
        entry(db, company, entity_type="filing", summary="a filing")
        entry(db, company, entity_type="document", summary="a document")

        header, rows = export(app_client, company_admin, entity_type="document")

        assert [r[header.index("summary")] for r in rows] == ["a document"]

    def test_an_empty_trail_exports_a_header_and_nothing_else(
        self, app_client, company_admin
    ):
        header, rows = export(app_client, company_admin)

        assert header[0] == "sequence"
        assert rows == []

    def test_a_null_column_is_written_empty_rather_than_as_the_word_none(
        self, app_client, db, company, company_admin
    ):
        """``None`` in a CSV cell reads as data. A finance team opening this in
        Excel would see a column of the string "None" and reasonably conclude
        somebody's name was None."""
        entry(db, company, user_id=None, entity_id=None, ip_address=None)

        header, rows = export(app_client, company_admin)
        row = rows[0]

        assert row[header.index("user_id")] == ""
        assert row[header.index("entity_id")] == ""
        assert row[header.index("ip_address")] == ""

    def test_the_export_is_bounded(self, app_client, db, company, company_admin, monkeypatch):
        """Past this size it is a data extract, not an audit review.

        Patched down rather than writing fifty thousand rows: the property is
        that the cap is applied, and asserting it at the real value would cost
        a minute a run to learn the same thing.
        """
        monkeypatch.setattr("app.routers.audit._MAX_EXPORT_ROWS", 2)
        for n in range(4):
            entry(db, company, summary=f"change {n}")

        _, rows = export(app_client, company_admin)

        assert len(rows) == 2


class TestExportDelegation:
    def test_a_firm_exporting_for_a_client_exports_the_clients_trail(
        self, app_client, db, ca_firm, firm_admin, company, engagement
    ):
        entry(db, company, summary="client history")
        entry(db, ca_firm, summary="firm history")

        response = app_client.get(
            f"{API}/audit/export", headers=auth(firm_admin, client_org_id=company.id)
        )
        header, *rows = list(csv.reader(io.StringIO(response.text)))

        assert [r[header.index("summary")] for r in rows] == ["client history"]

    def test_the_export_is_recorded_in_both_chains(
        self, app_client, db, ca_firm, firm_admin, company, engagement
    ):
        """A firm taking a full copy of a client's trail is a thing the client
        must be able to see happened, and the firm must be able to show who on
        their side did it."""
        app_client.get(
            f"{API}/audit/export", headers=auth(firm_admin, client_org_id=company.id)
        )

        db.expire_all()
        recorded = (
            db.query(AuditTrail).filter(AuditTrail.entity_type == "audit_trail").all()
        )
        assert {r.organization_id for r in recorded} == {company.id, ca_firm.id}

    def test_a_staff_member_capped_below_admin_cannot_export(
        self, app_client, db, ca_firm, firm_staff, company
    ):
        """The effective role is what the guard reads.

        A firm Admin whose assignment grants only Staff on this client is Staff
        here — the delegation caps, it does not carry the home role across.
        """
        make_engagement(db, ca_firm, company, assigned_user_id=firm_staff.id)

        response = app_client.get(
            f"{API}/audit/export", headers=auth(firm_staff, client_org_id=company.id)
        )

        assert response.status_code == 403
