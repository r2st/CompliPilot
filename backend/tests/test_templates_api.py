"""The template library: sharing, versioning, and rendering.

Three properties carry this router, and the file is organised around them.

**System rows are visible to everyone and writable by nobody.** The library
holds CompliPilot's templates (``organization_id IS NULL``) beside each
tenant's own, and a tenant may create a row with the same code — that is what
an override *is*. The tests check both halves: the union is what a tenant
browses, and an attempt to edit the shared row is refused with an explanation
rather than silently rewriting a form for every other firm on the platform.

**Versions are immutable.** Changing a template's field schema or body inserts
a new row at the next version and deactivates the old one. This is not
bookkeeping: a filing keeps a pointer to the row it was drafted against, so a
return submitted last year must reopen rendering the fields that existed then.
Editing in place would rewrite history for every filing pointing at it — so the
tests assert the old row survives, keeps its version, and is still readable.

**Rendering is a dictionary lookup, not ``str.format``.** A body is tenant
input and ``"{0.__class__}".format(x)`` is an attribute-traversal vector — the
classic Python format-string injection. The test posts exactly that and asserts
it comes back as inert text.
"""
from __future__ import annotations

from app.models.enums import Regulation, UserRole
from app.models.template import Template
from tests.conftest import (
    API,
    auth,
    make_template,
    make_user,
)

FILING = {
    "code": "acme.gst.internal",
    "name": "Internal GST working paper",
    "category": "filing",
    "template_json": {"fields": [{"key": "turnover", "label": "Turnover", "type": "money"}]},
}

DOCUMENT = {
    "code": "acme.doc.resolution",
    "name": "Board resolution — Acme house style",
    "category": "document",
    "body_template": "RESOLVED THAT {company_name} appoints {director_name}.",
}


class TestBrowsing:
    def test_the_system_library_is_visible_to_every_tenant(
        self, app_client, seeded, company_admin, other_admin
    ):
        mine = app_client.get(f"{API}/templates", headers=auth(company_admin)).json()
        theirs = app_client.get(f"{API}/templates", headers=auth(other_admin)).json()

        assert mine["total"] > 0
        assert mine["total"] == theirs["total"]

    def test_a_tenants_own_template_is_invisible_to_another_tenant(
        self, app_client, db, company, other_company, company_admin, other_admin
    ):
        make_template(db, organization_id=company.id, code="acme.private")

        mine = app_client.get(f"{API}/templates", headers=auth(company_admin)).json()
        theirs = app_client.get(f"{API}/templates", headers=auth(other_admin)).json()

        assert [t["code"] for t in mine["items"]] == ["acme.private"]
        assert theirs["total"] == 0

    def test_mine_only_excludes_the_shared_library(
        self, app_client, db, company, seeded, company_admin
    ):
        make_template(db, organization_id=company.id, code="acme.private")

        body = app_client.get(
            f"{API}/templates", headers=auth(company_admin), params={"mine_only": True}
        ).json()

        assert [t["code"] for t in body["items"]] == ["acme.private"]

    def test_inactive_rows_are_hidden_unless_asked_for(
        self, app_client, db, company, company_admin
    ):
        make_template(db, organization_id=company.id, is_active=False)

        default = app_client.get(f"{API}/templates", headers=auth(company_admin)).json()
        asked = app_client.get(
            f"{API}/templates",
            headers=auth(company_admin),
            params={"include_inactive": True},
        ).json()

        assert default["total"] == 0
        assert asked["total"] == 1

    def test_soft_deleted_rows_are_never_returned(
        self, app_client, db, company, company_admin
    ):
        template = make_template(db, organization_id=company.id)
        template.soft_delete()
        db.flush()

        body = app_client.get(
            f"{API}/templates",
            headers=auth(company_admin),
            params={"include_inactive": True},
        ).json()

        assert body["total"] == 0

    def test_filtering_by_regulation_category_and_filing_type(
        self, app_client, db, company, company_admin
    ):
        make_template(
            db,
            organization_id=company.id,
            code="a.gst",
            regulation=Regulation.GST,
            filing_type="GSTR-3B",
        )
        make_template(
            db, organization_id=company.id, code="a.doc", category="document",
            regulation=Regulation.MCA,
        )

        def total(**params):
            return app_client.get(
                f"{API}/templates", headers=auth(company_admin), params=params
            ).json()["total"]

        assert total(regulation="gst") == 1
        assert total(category="document") == 1
        assert total(filing_type="GSTR-3B") == 1

    def test_searching_matches_the_name_the_code_and_the_description(
        self, app_client, db, company, company_admin
    ):
        make_template(db, organization_id=company.id, code="a.one", name="Annual return")
        make_template(db, organization_id=company.id, code="acme.reconciliation", name="Two")
        make_template(
            db,
            organization_id=company.id,
            code="a.three",
            name="Three",
            description="Working paper for the audit file",
        )

        def total(term):
            return app_client.get(
                f"{API}/templates", headers=auth(company_admin), params={"search": term}
            ).json()["total"]

        assert total("annual") == 1
        assert total("reconciliation") == 1
        assert total("audit file") == 1
        assert total("nothing here") == 0

    def test_versions_of_one_code_come_back_newest_first(
        self, app_client, db, company, company_admin
    ):
        make_template(db, organization_id=company.id, code="a.form", version=1, is_active=False)
        make_template(db, organization_id=company.id, code="a.form", version=2)

        versions = [
            t["version"]
            for t in app_client.get(
                f"{API}/templates",
                headers=auth(company_admin),
                params={"include_inactive": True},
            ).json()["items"]
        ]

        assert versions == [2, 1]

    def test_pagination_reports_the_full_total(
        self, app_client, db, company, company_admin
    ):
        for _ in range(5):
            make_template(db, organization_id=company.id)

        body = app_client.get(
            f"{API}/templates", headers=auth(company_admin), params={"limit": 2}
        ).json()

        assert len(body["items"]) == 2
        assert body["total"] == 5

    def test_the_summary_omits_the_field_schema_and_the_body(
        self, app_client, db, company, company_admin
    ):
        make_template(db, organization_id=company.id)

        item = app_client.get(f"{API}/templates", headers=auth(company_admin)).json()[
            "items"
        ][0]

        assert "template_json" not in item
        assert "body_template" not in item

    def test_a_read_only_user_may_browse(self, app_client, db, company, company_reader):
        make_template(db, organization_id=company.id)

        response = app_client.get(f"{API}/templates", headers=auth(company_reader))

        assert response.status_code == 200
        assert response.json()["total"] == 1

    def test_it_needs_a_token(self, app_client):
        assert app_client.get(f"{API}/templates").status_code == 401


class TestResolvingByCode:
    def test_the_active_version_is_returned(self, app_client, db, company, company_admin):
        make_template(db, organization_id=company.id, code="a.form", version=1, is_active=False)
        make_template(db, organization_id=company.id, code="a.form", version=2)

        body = app_client.get(
            f"{API}/templates/by-code/a.form", headers=auth(company_admin)
        ).json()

        assert body["version"] == 2

    def test_an_older_version_can_be_asked_for_by_number(
        self, app_client, db, company, company_admin
    ):
        """What makes reopening last year's filing render last year's form."""
        make_template(db, organization_id=company.id, code="a.form", version=1, is_active=False)
        make_template(db, organization_id=company.id, code="a.form", version=2)

        body = app_client.get(
            f"{API}/templates/by-code/a.form",
            headers=auth(company_admin),
            params={"version": 1},
        ).json()

        assert body["version"] == 1

    def test_a_tenants_own_row_wins_over_the_system_one(
        self, app_client, db, company, company_admin
    ):
        make_template(db, code="gst.gstr3b", name="CompliPilot's")
        make_template(db, organization_id=company.id, code="gst.gstr3b", name="Acme's")

        body = app_client.get(
            f"{API}/templates/by-code/gst.gstr3b", headers=auth(company_admin)
        ).json()

        assert body["name"] == "Acme's"
        assert body["organization_id"] == company.id

    def test_a_tenant_without_an_override_still_gets_the_system_row(
        self, app_client, db, company, other_admin
    ):
        make_template(db, code="gst.gstr3b", name="CompliPilot's")
        make_template(db, organization_id=company.id, code="gst.gstr3b", name="Acme's")

        body = app_client.get(
            f"{API}/templates/by-code/gst.gstr3b", headers=auth(other_admin)
        ).json()

        assert body["name"] == "CompliPilot's"

    def test_an_unknown_code_is_a_404(self, app_client, company_admin):
        assert (
            app_client.get(
                f"{API}/templates/by-code/no.such.code", headers=auth(company_admin)
            ).status_code
            == 404
        )

    def test_another_tenants_code_is_a_404(
        self, app_client, db, other_company, company_admin
    ):
        make_template(db, organization_id=other_company.id, code="rival.secret")

        response = app_client.get(
            f"{API}/templates/by-code/rival.secret", headers=auth(company_admin)
        )

        assert response.status_code == 404


class TestReadingOne:
    def test_the_detail_carries_the_field_schema(
        self, app_client, db, company, company_admin
    ):
        template = make_template(db, organization_id=company.id)

        body = app_client.get(
            f"{API}/templates/{template.id}", headers=auth(company_admin)
        ).json()

        assert body["template_json"]["fields"][0]["key"] == "turnover"

    def test_a_system_template_is_readable(self, app_client, db, company_admin):
        template = make_template(db, code="gst.gstr3b")

        response = app_client.get(
            f"{API}/templates/{template.id}", headers=auth(company_admin)
        )

        assert response.status_code == 200
        assert response.json()["is_system"] is True

    def test_another_tenants_template_is_a_404(
        self, app_client, db, other_company, company_admin
    ):
        theirs = make_template(db, organization_id=other_company.id)

        response = app_client.get(
            f"{API}/templates/{theirs.id}", headers=auth(company_admin)
        )

        assert response.status_code == 404


class TestCreation:
    def test_a_manager_can_add_one(self, app_client, db, company, company_admin):
        response = app_client.post(
            f"{API}/templates", headers=auth(company_admin), json=FILING
        )

        assert response.status_code == 201
        body = response.json()
        assert body["version"] == 1
        assert body["is_active"] is True
        assert body["is_system"] is False
        assert body["organization_id"] == company.id

    def test_staff_cannot(self, app_client, company_staff):
        response = app_client.post(
            f"{API}/templates", headers=auth(company_staff), json=FILING
        )

        assert response.status_code == 403

    def test_a_duplicate_code_within_the_tenant_is_a_conflict(
        self, app_client, db, company, company_admin
    ):
        make_template(db, organization_id=company.id, code=FILING["code"])

        response = app_client.post(
            f"{API}/templates", headers=auth(company_admin), json=FILING
        )

        assert response.status_code == 409
        assert "edit it to create a new version" in response.json()["error"]["message"]

    def test_the_same_code_at_another_tenant_is_fine(
        self, app_client, db, other_company, company_admin
    ):
        make_template(db, organization_id=other_company.id, code=FILING["code"])

        response = app_client.post(
            f"{API}/templates", headers=auth(company_admin), json=FILING
        )

        assert response.status_code == 201

    def test_a_code_colliding_with_a_system_row_is_an_override_not_a_conflict(
        self, app_client, db, company_admin
    ):
        make_template(db, code="gst.gstr3b")

        response = app_client.post(
            f"{API}/templates",
            headers=auth(company_admin),
            json={**FILING, "code": "gst.gstr3b"},
        )

        assert response.status_code == 201

    def test_a_filing_template_without_a_field_schema_is_refused(
        self, app_client, company_admin
    ):
        payload = {k: v for k, v in FILING.items() if k != "template_json"}

        response = app_client.post(
            f"{API}/templates", headers=auth(company_admin), json=payload
        )

        assert response.status_code == 422

    def test_a_document_template_without_a_body_is_refused(
        self, app_client, company_admin
    ):
        payload = {k: v for k, v in DOCUMENT.items() if k != "body_template"}

        response = app_client.post(
            f"{API}/templates", headers=auth(company_admin), json=payload
        )

        assert response.status_code == 422

    def test_a_field_schema_whose_fields_are_not_a_list_is_refused(
        self, app_client, company_admin
    ):
        response = app_client.post(
            f"{API}/templates",
            headers=auth(company_admin),
            json={**FILING, "template_json": {"fields": {"turnover": "money"}}},
        )

        assert response.status_code == 422

    def test_an_uppercase_code_is_refused_by_the_pattern(self, app_client, company_admin):
        response = app_client.post(
            f"{API}/templates", headers=auth(company_admin), json={**FILING, "code": "Acme.GST"}
        )

        assert response.status_code == 422

    def test_creation_is_audited(self, app_client, db, company_admin):
        from app.models.audit import AuditTrail

        app_client.post(f"{API}/templates", headers=auth(company_admin), json=FILING)

        entry = db.query(AuditTrail).filter_by(entity_type="template").one()
        assert str(entry.action) == "create"
        assert entry.after_json["code"] == FILING["code"]
        assert entry.after_json["version"] == 1


class TestVersioning:
    def test_changing_the_field_schema_creates_a_new_version(
        self, app_client, db, company, company_admin
    ):
        template = make_template(db, organization_id=company.id, code="a.form")

        body = app_client.patch(
            f"{API}/templates/{template.id}",
            headers=auth(company_admin),
            json={"template_json": {"fields": [{"key": "itc", "label": "ITC", "type": "money"}]}},
        ).json()

        assert body["version"] == 2
        assert body["id"] != template.id
        assert body["is_active"] is True

    def test_the_edited_version_survives_and_is_deactivated(
        self, app_client, db, company, company_admin
    ):
        template = make_template(db, organization_id=company.id, code="a.form")
        original_schema = dict(template.template_json)

        app_client.patch(
            f"{API}/templates/{template.id}",
            headers=auth(company_admin),
            json={"template_json": {"fields": []}},
        )

        db.refresh(template)
        # The row a filing points at is untouched: same id, same version, same
        # fields. Only its activity flag moved.
        assert template.version == 1
        assert template.template_json == original_schema
        assert template.is_active is False
        assert template.deleted_at is None

    def test_changing_a_document_body_also_versions(
        self, app_client, db, company, company_admin
    ):
        template = make_template(db, organization_id=company.id, category="document")

        body = app_client.patch(
            f"{API}/templates/{template.id}",
            headers=auth(company_admin),
            json={"body_template": "A wholly new body for {company_name}."},
        ).json()

        assert body["version"] == 2
        assert body["body_template"] == "A wholly new body for {company_name}."

    def test_a_presentation_change_is_applied_in_place(
        self, app_client, db, company, company_admin
    ):
        template = make_template(db, organization_id=company.id, name="Before")

        body = app_client.patch(
            f"{API}/templates/{template.id}",
            headers=auth(company_admin),
            json={"name": "After", "description": "A clearer explanation"},
        ).json()

        assert body["id"] == template.id
        assert body["version"] == 1
        assert body["name"] == "After"
        assert db.query(Template).count() == 1

    def test_resubmitting_the_same_content_does_not_version(
        self, app_client, db, company, company_admin
    ):
        """A save button that fires on every blur must not walk the version."""
        template = make_template(db, organization_id=company.id)

        body = app_client.patch(
            f"{API}/templates/{template.id}",
            headers=auth(company_admin),
            json={"template_json": template.template_json},
        ).json()

        assert body["version"] == 1
        assert db.query(Template).count() == 1

    def test_the_new_version_continues_from_the_highest_not_the_edited_one(
        self, app_client, db, company, company_admin
    ):
        """Editing v1 when a v2 exists must land on v3, not collide at v2."""
        first = make_template(db, organization_id=company.id, code="a.form", version=1)
        make_template(
            db, organization_id=company.id, code="a.form", version=2, is_active=False
        )

        body = app_client.patch(
            f"{API}/templates/{first.id}",
            headers=auth(company_admin),
            json={"template_json": {"fields": [{"key": "x", "label": "X", "type": "text"}]}},
        ).json()

        assert body["version"] == 3

    def test_a_new_version_carries_the_unchanged_fields_forward(
        self, app_client, db, company, company_admin
    ):
        template = make_template(
            db,
            organization_id=company.id,
            regulation=Regulation.GST,
            filing_type="GSTR-3B",
            instructions="File by the 20th",
            statutory_reference="Section 39",
        )

        body = app_client.patch(
            f"{API}/templates/{template.id}",
            headers=auth(company_admin),
            json={"template_json": {"fields": []}},
        ).json()

        assert body["regulation"] == "gst"
        assert body["filing_type"] == "GSTR-3B"
        assert body["instructions"] == "File by the 20th"
        assert body["statutory_reference"] == "Section 39"

    def test_the_version_bump_is_audited_against_the_row_it_replaced(
        self, app_client, db, company, company_admin
    ):
        from app.models.audit import AuditTrail

        template = make_template(db, organization_id=company.id, code="a.form")

        app_client.patch(
            f"{API}/templates/{template.id}",
            headers=auth(company_admin),
            json={"template_json": {"fields": []}},
        )

        entry = db.query(AuditTrail).filter_by(entity_type="template").one()
        assert str(entry.action) == "create"
        assert entry.before_json == {"version": 1, "template_id": template.id}
        assert entry.after_json["version"] == 2

    def test_a_no_op_patch_writes_no_audit_entry(
        self, app_client, db, company, company_admin
    ):
        from app.models.audit import AuditTrail

        template = make_template(db, organization_id=company.id, name="Unchanged")

        response = app_client.patch(
            f"{API}/templates/{template.id}",
            headers=auth(company_admin),
            json={"name": "Unchanged"},
        )

        assert response.status_code == 200
        assert db.query(AuditTrail).filter_by(entity_type="template").count() == 0

    def test_a_template_can_be_deactivated_without_versioning(
        self, app_client, db, company, company_admin
    ):
        template = make_template(db, organization_id=company.id)

        body = app_client.patch(
            f"{API}/templates/{template.id}",
            headers=auth(company_admin),
            json={"is_active": False},
        ).json()

        assert body["id"] == template.id
        assert body["is_active"] is False

    def test_a_system_template_cannot_be_edited(self, app_client, db, company_admin):
        system = make_template(db, code="gst.gstr3b")

        response = app_client.patch(
            f"{API}/templates/{system.id}",
            headers=auth(company_admin),
            json={"name": "Acme's version"},
        )

        assert response.status_code == 403
        assert "maintained by CompliPilot" in response.json()["error"]["message"]

    def test_another_tenants_template_is_a_404_not_a_403(
        self, app_client, db, other_company, company_admin
    ):
        """403 would confirm the id exists, and the id space is walkable."""
        theirs = make_template(db, organization_id=other_company.id)

        response = app_client.patch(
            f"{API}/templates/{theirs.id}",
            headers=auth(company_admin),
            json={"name": "Mine now"},
        )

        assert response.status_code == 404

    def test_staff_cannot_edit(self, app_client, db, company, company_staff):
        template = make_template(db, organization_id=company.id)

        response = app_client.patch(
            f"{API}/templates/{template.id}",
            headers=auth(company_staff),
            json={"name": "Nope"},
        )

        assert response.status_code == 403


class TestRendering:
    def test_placeholders_are_filled(self, app_client, db, company, company_admin):
        template = make_template(
            db,
            organization_id=company.id,
            category="document",
            body_template="RESOLVED THAT {company_name} appoints {director_name}.",
        )

        body = app_client.post(
            f"{API}/templates/{template.id}/render",
            headers=auth(company_admin),
            json={"values": {"company_name": "Acme Pvt Ltd", "director_name": "Priya Sharma"}},
        ).json()

        assert body["body"] == "RESOLVED THAT Acme Pvt Ltd appoints Priya Sharma."
        assert body["missing"] == []
        assert body["code"] == template.code
        assert body["version"] == 1

    def test_an_unfilled_placeholder_is_left_visible_and_reported(
        self, app_client, db, company, company_admin
    ):
        """A visible {director_name} tells the drafter what is missing. A silent
        gap where the name belongs gets signed."""
        template = make_template(
            db,
            organization_id=company.id,
            category="document",
            body_template="RESOLVED THAT {company_name} appoints {director_name}.",
        )

        body = app_client.post(
            f"{API}/templates/{template.id}/render",
            headers=auth(company_admin),
            json={"values": {"company_name": "Acme Pvt Ltd"}},
        ).json()

        assert "{director_name}" in body["body"]
        assert body["missing"] == ["director_name"]

    def test_an_explicit_null_counts_as_missing(
        self, app_client, db, company, company_admin
    ):
        template = make_template(
            db,
            organization_id=company.id,
            category="document",
            body_template="Signed by {signatory_name}.",
        )

        body = app_client.post(
            f"{API}/templates/{template.id}/render",
            headers=auth(company_admin),
            json={"values": {"signatory_name": None}},
        ).json()

        assert body["missing"] == ["signatory_name"]
        assert "{signatory_name}" in body["body"]

    def test_a_number_is_rendered_as_text(self, app_client, db, company, company_admin):
        template = make_template(
            db,
            organization_id=company.id,
            category="document",
            body_template="DIN: {din}",
        )

        body = app_client.post(
            f"{API}/templates/{template.id}/render",
            headers=auth(company_admin),
            json={"values": {"din": 12345678}},
        ).json()

        assert body["body"] == "DIN: 12345678"

    def test_a_format_string_payload_in_the_body_is_inert(
        self, app_client, db, company, company_admin
    ):
        """``"{0.__class__}".format(x)`` is an attribute-traversal vector.

        Substitution here is a dictionary lookup against a name pattern that
        cannot express a dotted path or an index, so the payload survives as
        text rather than reaching an object.
        """
        template = make_template(
            db,
            organization_id=company.id,
            category="document",
            body_template="{0.__class__.__mro__} and {values.__globals__} for {company_name}",
        )

        body = app_client.post(
            f"{API}/templates/{template.id}/render",
            headers=auth(company_admin),
            json={"values": {"company_name": "Acme"}},
        ).json()

        assert body["body"].startswith("{0.__class__.__mro__} and {values.__globals__}")
        assert body["body"].endswith("for Acme")
        # Nothing traversed: the dotted forms are not even recognised as names
        # to fill, so they are not reported as missing either.
        assert body["missing"] == []

    def test_a_value_containing_braces_is_not_re_expanded(
        self, app_client, db, company, company_admin
    ):
        """One pass, not a fixed point — otherwise a value could inject a name."""
        template = make_template(
            db,
            organization_id=company.id,
            category="document",
            body_template="For {company_name}.",
        )

        body = app_client.post(
            f"{API}/templates/{template.id}/render",
            headers=auth(company_admin),
            json={"values": {"company_name": "{secret}", "secret": "leaked"}},
        ).json()

        assert body["body"] == "For {secret}."

    def test_a_filing_template_has_nothing_to_render(
        self, app_client, db, company, company_admin
    ):
        template = make_template(db, organization_id=company.id, category="filing")

        response = app_client.post(
            f"{API}/templates/{template.id}/render",
            headers=auth(company_admin),
            json={"values": {}},
        )

        assert response.status_code == 422
        assert response.json()["error"]["details"]["category"] == "filing"

    def test_a_system_template_may_be_rendered(self, app_client, db, company_admin):
        system = make_template(
            db, code="doc.board_resolution", category="document",
            body_template="For {company_name}.",
        )

        response = app_client.post(
            f"{API}/templates/{system.id}/render",
            headers=auth(company_admin),
            json={"values": {"company_name": "Acme"}},
        )

        assert response.status_code == 200

    def test_a_read_only_user_may_render(self, app_client, db, company, company_reader):
        """Drafting a reply is reading the library, not changing it."""
        template = make_template(
            db, organization_id=company.id, category="document",
            body_template="For {company_name}.",
        )

        response = app_client.post(
            f"{API}/templates/{template.id}/render",
            headers=auth(company_reader),
            json={"values": {"company_name": "Acme"}},
        )

        assert response.status_code == 200

    def test_another_tenants_template_cannot_be_rendered(
        self, app_client, db, other_company, company_admin
    ):
        theirs = make_template(db, organization_id=other_company.id, category="document")

        response = app_client.post(
            f"{API}/templates/{theirs.id}/render",
            headers=auth(company_admin),
            json={"values": {}},
        )

        assert response.status_code == 404


class TestPlaceholders:
    def test_they_come_back_in_order_and_de_duplicated(
        self, app_client, db, company, company_admin
    ):
        template = make_template(
            db,
            organization_id=company.id,
            category="document",
            body_template="For {company_name}, signed {signatory}. For {company_name}.",
        )

        names = app_client.get(
            f"{API}/templates/{template.id}/placeholders", headers=auth(company_admin)
        ).json()

        assert names == ["company_name", "signatory"]

    def test_a_filing_template_declares_none(
        self, app_client, db, company, company_admin
    ):
        template = make_template(db, organization_id=company.id, category="filing")

        names = app_client.get(
            f"{API}/templates/{template.id}/placeholders", headers=auth(company_admin)
        ).json()

        assert names == []

    def test_a_dotted_payload_is_not_a_placeholder(
        self, app_client, db, company, company_admin
    ):
        template = make_template(
            db,
            organization_id=company.id,
            category="document",
            body_template="{0.__class__} {company_name}",
        )

        names = app_client.get(
            f"{API}/templates/{template.id}/placeholders", headers=auth(company_admin)
        ).json()

        assert names == ["company_name"]


class TestRetirement:
    def test_retiring_soft_deletes_and_deactivates(
        self, app_client, db, company, company_admin
    ):
        template = make_template(db, organization_id=company.id)

        response = app_client.delete(
            f"{API}/templates/{template.id}", headers=auth(company_admin)
        )

        assert response.status_code == 200
        db.refresh(template)
        assert template.deleted_at is not None
        assert template.is_active is False

    def test_the_audit_entry_records_the_state_before_the_retirement(
        self, app_client, db, company, company_admin
    ):
        """``before`` must be the state that was lost.

        An entry saying the template was already inactive when it was retired
        describes a different event from the one that happened, and the trail
        is only evidence if it says what changed.
        """
        from app.models.audit import AuditTrail

        template = make_template(db, organization_id=company.id, code="a.form", is_active=True)

        app_client.delete(f"{API}/templates/{template.id}", headers=auth(company_admin))

        entry = (
            db.query(AuditTrail)
            .filter_by(entity_type="template", action="soft_delete")
            .one()
        )
        assert entry.before_json["is_active"] is True
        assert entry.before_json["code"] == "a.form"

    def test_a_retired_template_is_still_readable_by_id(
        self, app_client, db, company, company_admin
    ):
        """Reopening last year's filing must still render last year's form.

        The filing keeps its ``template_id`` and the row is only soft deleted,
        so resolving it by id has to keep working — otherwise retiring a
        template breaks every filing ever drafted against it.
        """
        template = make_template(db, organization_id=company.id, code="a.form")
        app_client.delete(f"{API}/templates/{template.id}", headers=auth(company_admin))

        response = app_client.get(
            f"{API}/templates/{template.id}", headers=auth(company_admin)
        )

        assert response.status_code == 200
        assert response.json()["code"] == "a.form"

    def test_a_retired_document_template_can_still_be_rendered(
        self, app_client, db, company, company_admin
    ):
        template = make_template(
            db, organization_id=company.id, category="document",
            body_template="For {company_name}.",
        )
        app_client.delete(f"{API}/templates/{template.id}", headers=auth(company_admin))

        response = app_client.post(
            f"{API}/templates/{template.id}/render",
            headers=auth(company_admin),
            json={"values": {"company_name": "Acme"}},
        )

        assert response.status_code == 200
        assert response.json()["body"] == "For Acme."

    def test_a_retired_template_is_gone_from_the_library(
        self, app_client, db, company, company_admin
    ):
        template = make_template(db, organization_id=company.id)

        app_client.delete(f"{API}/templates/{template.id}", headers=auth(company_admin))

        body = app_client.get(
            f"{API}/templates",
            headers=auth(company_admin),
            params={"include_inactive": True},
        ).json()
        assert body["total"] == 0

    def test_a_retired_template_cannot_be_edited(
        self, app_client, db, company, company_admin
    ):
        template = make_template(db, organization_id=company.id)
        app_client.delete(f"{API}/templates/{template.id}", headers=auth(company_admin))

        response = app_client.patch(
            f"{API}/templates/{template.id}",
            headers=auth(company_admin),
            json={"name": "Back from the dead"},
        )

        assert response.status_code == 404

    def test_the_code_can_be_used_again_after_retirement(
        self, app_client, db, company, company_admin
    ):
        make_template(db, organization_id=company.id, code=FILING["code"])
        first = db.query(Template).one()
        app_client.delete(f"{API}/templates/{first.id}", headers=auth(company_admin))

        response = app_client.post(
            f"{API}/templates", headers=auth(company_admin), json=FILING
        )

        assert response.status_code == 201

    def test_a_system_template_cannot_be_retired(self, app_client, db, company_admin):
        system = make_template(db, code="gst.gstr3b")

        response = app_client.delete(
            f"{API}/templates/{system.id}", headers=auth(company_admin)
        )

        assert response.status_code == 403

    def test_another_tenants_template_cannot_be_retired(
        self, app_client, db, other_company, company_admin
    ):
        theirs = make_template(db, organization_id=other_company.id)

        response = app_client.delete(
            f"{API}/templates/{theirs.id}", headers=auth(company_admin)
        )

        assert response.status_code == 404

    def test_staff_cannot_retire(self, app_client, db, company, company_staff):
        template = make_template(db, organization_id=company.id)

        response = app_client.delete(
            f"{API}/templates/{template.id}", headers=auth(company_staff)
        )

        assert response.status_code == 403

    def test_a_compliance_manager_may_retire(self, app_client, db, company):
        manager = make_user(db, company, role=UserRole.COMPLIANCE_MANAGER)
        template = make_template(db, organization_id=company.id)

        response = app_client.delete(
            f"{API}/templates/{template.id}", headers=auth(manager)
        )

        assert response.status_code == 200
