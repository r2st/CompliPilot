"""Regulatory update analysis and per-tenant impact mapping.

The impact mapper is the piece that decides who hears about a circular. Both
directions of a mistake are expensive: a missed match means a client is not
told about a rule that binds them, and a spurious one puts a SEBI circular on a
proprietorship's alert list until they stop reading the list. So most of what
follows is a pair — this profile matches, that one does not.

The mapper makes no model calls, which is what lets it be tested this directly:
the analysis is stored on the update, and matching it against a profile is
ordinary code.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.models.enums import EntityType, ImpactLevel, OrgType, Regulation, UserRole
from app.models.notification import Notification
from app.models.regulatory import RegulatoryImpact, RegulatoryUpdate
from app.services import llm
from app.tasks import regulatory_tasks
from tests.conftest import CRORE, make_org, make_user

TODAY = date(2026, 8, 8)


def _result(data: dict) -> llm.LLMResult:
    """A completion as :func:`app.services.llm.complete_json` returns one."""
    return llm.LLMResult(
        content="{}", model="test-model", prompt_tokens=10, completion_tokens=5, data=data
    )


def _model_returns(monkeypatch, data: dict) -> None:
    """Configure a model, and make it answer *data*.

    The analyser is exercised here rather than the client: what the model says
    is fixed so that the tests are about what the update row does with it.
    ``llm.complete_json`` has its own tests, including the ones about a model
    that returns prose where JSON was asked for.
    """
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "complete_json", lambda *_a, **_k: _result(data))


def make_update(
    db,
    *,
    title: str = "Amendment to GST return filing",
    source: str = "cbic",
    impact_level: ImpactLevel = ImpactLevel.HIGH,
    entity_types: list[str] | None = None,
    states: list[str] | None = None,
    forms: list[str] | None = None,
    turnover_threshold_inr: float | None = None,
    **kwargs,
) -> RegulatoryUpdate:
    update = RegulatoryUpdate(
        source=source,
        title=title,
        published_date=kwargs.pop("published_date", TODAY),
        impact_level=impact_level,
        regulation=kwargs.pop("regulation", Regulation.GST),
        affected_entity_types_json=entity_types,
        affected_states_json=states,
        affected_obligation_codes_json=forms,
        analysis_json=(
            {"turnover_threshold_inr": turnover_threshold_inr}
            if turnover_threshold_inr is not None
            else None
        ),
        summary=kwargs.pop("summary", "The return due date has moved."),
        action_required=kwargs.pop("action_required", "File by the new date."),
        is_analysed=kwargs.pop("is_analysed", True),
        **kwargs,
    )
    db.add(update)
    db.flush()
    return update


class TestEntityTypeMatching:
    def test_an_update_with_no_entity_filter_reaches_everyone(self, db, company):
        update = make_update(db)
        assert regulatory_tasks.assess_impact(db, company, update) is not None

    def test_a_named_entity_type_matches(self, db, company):
        update = make_update(db, entity_types=["private_limited"])

        result = regulatory_tasks.assess_impact(db, company, update)

        assert result is not None
        assert "private_limited" in result[1]

    def test_an_unnamed_entity_type_does_not_match(self, db):
        """A SEBI circular for listed companies is noise to a proprietorship."""
        sole = make_org(db, name="Kumar Traders", entity_type=EntityType.PROPRIETORSHIP)
        update = make_update(db, entity_types=["private_limited", "public_limited"])

        assert regulatory_tasks.assess_impact(db, sole, update) is None

    def test_an_organization_with_no_entity_type_does_not_match_a_filtered_update(
        self, db
    ):
        unknown = make_org(db, name="Unspecified Ltd", entity_type=None)
        update = make_update(db, entity_types=["private_limited"])

        assert regulatory_tasks.assess_impact(db, unknown, update) is None


class TestStateMatching:
    def test_a_named_state_matches_case_insensitively(self, db, company):
        update = make_update(db, states=["karnataka"])

        result = regulatory_tasks.assess_impact(db, company, update)

        assert result is not None

    def test_another_state_does_not_match(self, db, company):
        update = make_update(db, states=["maharashtra"])

        assert regulatory_tasks.assess_impact(db, company, update) is None

    def test_an_all_india_update_reaches_every_state(self, db, company):
        update = make_update(db, states=[])

        assert regulatory_tasks.assess_impact(db, company, update) is not None


class TestTurnoverThreshold:
    def test_turnover_at_or_above_the_threshold_matches(self, db, company):
        # The company fixture turns over ₹5 crore.
        update = make_update(db, turnover_threshold_inr=20_000_000)  # ₹2 Cr

        result = regulatory_tasks.assess_impact(db, company, update)

        assert result is not None
        assert "threshold" in result[1].lower()

    def test_turnover_below_the_threshold_does_not_match(self, db):
        small = make_org(db, name="Small Co", annual_turnover_paise=1 * CRORE)
        update = make_update(db, turnover_threshold_inr=50_000_000)  # ₹5 Cr

        assert regulatory_tasks.assess_impact(db, small, update) is None

    def test_exactly_at_the_threshold_matches(self, db):
        """"At or above" — the reading that over-warns rather than under."""
        exact = make_org(db, name="Exact Co", annual_turnover_paise=5 * CRORE)
        update = make_update(db, turnover_threshold_inr=50_000_000)

        assert regulatory_tasks.assess_impact(db, exact, update) is not None

    def test_unknown_turnover_is_reported_at_low_rather_than_dropped(self, db):
        """The client may well be affected, and silence cannot be checked."""
        unknown = make_org(db, name="Unknown Co", annual_turnover_paise=None)
        update = make_update(db, turnover_threshold_inr=50_000_000)

        result = regulatory_tasks.assess_impact(db, unknown, update)

        assert result is not None
        level, rationale, _ = result
        assert level == ImpactLevel.LOW
        assert "not recorded" in rationale


class TestFormMatching:
    def test_an_update_naming_a_form_on_the_calendar_keeps_its_level(
        self, db, company, seeded
    ):
        from app.services.applicability import sync_organization_obligations

        sync_organization_obligations(db, company, on=TODAY)
        update = make_update(db, forms=["gstr-3b"], impact_level=ImpactLevel.HIGH)

        result = regulatory_tasks.assess_impact(db, company, update)

        assert result is not None
        level, rationale, obligation_ids = result
        assert level == ImpactLevel.HIGH
        assert obligation_ids
        assert "affected" in rationale

    def test_an_update_naming_a_form_nobody_files_is_stepped_down(self, db, company):
        """Keeps "critical" meaning something."""
        update = make_update(db, forms=["form-xyz-99"], impact_level=ImpactLevel.HIGH)

        result = regulatory_tasks.assess_impact(db, company, update)

        assert result is not None
        level, rationale, obligation_ids = result
        assert level == ImpactLevel.LOW
        assert obligation_ids == []
        assert "none of the named forms" in rationale.lower()

    def test_a_critical_update_steps_down_only_to_medium(self, db, company):
        update = make_update(db, forms=["form-xyz-99"], impact_level=ImpactLevel.CRITICAL)

        result = regulatory_tasks.assess_impact(db, company, update)

        assert result is not None
        assert result[0] == ImpactLevel.MEDIUM


class TestImpactFanOut:
    def test_a_matching_organization_gets_an_impact_row(self, db, company, company_admin):
        update = make_update(db)
        db.commit()

        result = regulatory_tasks.map_update_impacts(db, update)

        assert result["created"] == 1
        rows = db.query(RegulatoryImpact).filter_by(organization_id=company.id).all()
        assert len(rows) == 1
        assert rows[0].update_id == update.id

    def test_remapping_updates_rather_than_duplicating(self, db, company, company_admin):
        """An alert list that grew by one entry per mapper run is unusable."""
        update = make_update(db)
        db.commit()

        regulatory_tasks.map_update_impacts(db, update)
        second = regulatory_tasks.map_update_impacts(db, update)

        assert second["created"] == 0
        assert db.query(RegulatoryImpact).filter_by(update_id=update.id).count() == 1

    def test_a_non_matching_organization_gets_nothing(self, db, company, other_company):
        update = make_update(db, states=["karnataka"])  # company only
        db.commit()

        regulatory_tasks.map_update_impacts(db, update)

        assert (
            db.query(RegulatoryImpact)
            .filter_by(organization_id=other_company.id)
            .count()
            == 0
        )

    def test_the_rationale_is_stored_for_the_officer_to_check(self, db, company):
        update = make_update(db, entity_types=["private_limited"])
        db.commit()

        regulatory_tasks.map_update_impacts(db, update)

        impact = db.query(RegulatoryImpact).filter_by(organization_id=company.id).one()
        assert impact.rationale
        assert "private_limited" in impact.rationale

    def test_the_update_is_marked_published_after_the_fan_out(self, db, company):
        update = make_update(db)
        db.commit()

        regulatory_tasks.map_update_impacts(db, update)

        assert update.is_published

    def test_an_inactive_organization_is_not_mapped(self, db, company):
        company.is_active = False
        update = make_update(db)
        db.commit()

        result = regulatory_tasks.map_update_impacts(db, update)

        assert result["matched"] == 0


class TestImpactNotification:
    def _notifications(self, db, org):
        return (
            db.query(Notification)
            .filter(
                Notification.organization_id == org.id,
                Notification.kind == "regulatory_alert",
            )
            .all()
        )

    def test_a_high_impact_update_notifies(self, db, company, company_admin):
        update = make_update(db, impact_level=ImpactLevel.HIGH)
        db.commit()

        regulatory_tasks.map_update_impacts(db, update)

        assert self._notifications(db, company)

    def test_a_low_impact_update_does_not_interrupt_anyone(self, db, company, company_admin):
        """It lands in the alert list to be read when convenient."""
        update = make_update(db, impact_level=ImpactLevel.LOW)
        db.commit()

        regulatory_tasks.map_update_impacts(db, update)

        assert self._notifications(db, company) == []
        assert db.query(RegulatoryImpact).filter_by(organization_id=company.id).count() == 1

    def test_notification_happens_once_across_remaps(self, db, company, company_admin):
        update = make_update(db, impact_level=ImpactLevel.CRITICAL)
        db.commit()

        regulatory_tasks.map_update_impacts(db, update)
        first = len(self._notifications(db, company))
        regulatory_tasks.map_update_impacts(db, update)

        assert first > 0
        assert len(self._notifications(db, company)) == first

    def test_only_admins_and_compliance_managers_are_told(self, db, company):
        staff = make_user(db, company, role=UserRole.STAFF)
        admin = make_user(db, company, role=UserRole.ADMIN)
        update = make_update(db, impact_level=ImpactLevel.CRITICAL)
        db.commit()

        regulatory_tasks.map_update_impacts(db, update)

        reached = {n.recipient for n in self._notifications(db, company)}
        assert admin.email in reached
        assert staff.email not in reached

    def test_the_alert_carries_the_rationale(self, db, company, company_admin):
        update = make_update(db, entity_types=["private_limited"], impact_level=ImpactLevel.HIGH)
        db.commit()

        regulatory_tasks.map_update_impacts(db, update)

        body = self._notifications(db, company)[0].content
        assert "Why this reaches you" in body
        assert "File by the new date." in body


class TestAnalysisWithoutAKey:
    def test_an_unkeyed_deployment_marks_the_update_analysed_and_moves_on(
        self, db, monkeypatch
    ):
        """Otherwise the backlog grows without bound and nothing is ever mapped."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "openrouter_api_key", "")
        update = make_update(db, is_analysed=False)

        stored = regulatory_tasks.analyse_update(db, update)

        assert not stored
        assert update.is_analysed
        assert (update.analysis_json or {}).get("_skipped")


class TestAnalysisWithAModel:
    """What a returned analysis is allowed to change on the update.

    The model's job here is narrow: read a circular and say who it binds and
    by when. Everything it returns lands on a row that then drives a fan-out to
    every tenant, so the fields it may write, and the ones it may not, matter
    more than the wording of any of them.
    """

    def test_the_analysis_is_stored_with_its_provenance(self, db, monkeypatch):
        """An impact assessment is the kind of thing a client asks about six
        months later. "Which model said this, on how many tokens" has to be
        answerable from the row rather than from a log that has rotated."""
        update = make_update(db, is_analysed=False, summary=None)
        _model_returns(
            monkeypatch,
            {
                "summary": "The GSTR-3B due date moves to the 22nd.",
                "impact_level": "high",
                "regulation": "gst",
                "action_required": "File by the 22nd from September.",
            },
        )

        assert regulatory_tasks.analyse_update(db, update) is True

        assert update.summary == "The GSTR-3B due date moves to the 22nd."
        assert update.impact_level == ImpactLevel.HIGH
        assert update.action_required == "File by the 22nd from September."
        assert (update.analysis_json or {})["_model"] == "test-model"
        assert (update.analysis_json or {})["_tokens"] == 15

    def test_it_is_marked_analysed_so_the_sweep_moves_on(self, db, monkeypatch):
        update = make_update(db, is_analysed=False)
        _model_returns(monkeypatch, {"summary": "Something changed."})

        regulatory_tasks.analyse_update(db, update)

        assert update.is_analysed is True
        assert update.analysed_at is not None

    def test_the_audience_filters_are_lowercased(self, db, monkeypatch):
        """The matcher compares against lowercase profile values, so a model
        answering "Karnataka" must not produce an update that matches nobody —
        a filter that silently matches no one is indistinguishable from a
        circular that affects no one."""
        update = make_update(db, is_analysed=False)
        _model_returns(
            monkeypatch,
            {
                "summary": "State-specific amendment.",
                "affected_entity_types": ["Private_Limited"],
                "affected_states": ["Karnataka", " Kerala "],
                "affected_forms": ["GSTR-3B"],
            },
        )

        regulatory_tasks.analyse_update(db, update)

        assert update.affected_entity_types_json == ["private_limited"]
        assert update.affected_states_json == ["karnataka", "kerala"]
        assert update.affected_obligation_codes_json == ["gstr-3b"]

    @pytest.mark.parametrize("raw", [None, "not a list", 42, {"a": 1}])
    def test_a_malformed_audience_filter_becomes_no_filter(self, db, monkeypatch, raw):
        """Not a crash, and not a filter matching the string it was handed.

        An empty list is read downstream as "affects everyone", which is the
        safe reading: over-reporting one circular is recoverable, and a
        malformed field silently narrowing the audience to nobody is not.
        """
        update = make_update(db, is_analysed=False)
        _model_returns(
            monkeypatch, {"summary": "Something changed.", "affected_states": raw}
        )

        regulatory_tasks.analyse_update(db, update)

        assert update.affected_states_json == []

    def test_an_unusable_severity_falls_back_to_medium(self, db, monkeypatch):
        update = make_update(db, is_analysed=False)
        _model_returns(
            monkeypatch, {"summary": "Something changed.", "impact_level": "apocalyptic"}
        )

        regulatory_tasks.analyse_update(db, update)

        assert update.impact_level == ImpactLevel.MEDIUM

    def test_an_unusable_regulation_leaves_the_ingested_one_alone(self, db, monkeypatch):
        """The ingester knew which feed it read this from. A model that cannot
        name the regulation is less informed than the source, not more."""
        update = make_update(db, is_analysed=False, regulation=Regulation.GST)
        _model_returns(
            monkeypatch, {"summary": "Something changed.", "regulation": "cryptocurrency"}
        )

        regulatory_tasks.analyse_update(db, update)

        assert update.regulation == Regulation.GST

    def test_an_empty_summary_does_not_erase_the_one_already_there(
        self, db, monkeypatch
    ):
        """The feed's own abstract is better than nothing on a client's alert
        list."""
        update = make_update(db, is_analysed=False, summary="From the feed.")
        _model_returns(monkeypatch, {"summary": ""})

        regulatory_tasks.analyse_update(db, update)

        assert update.summary == "From the feed."

    def test_a_hallucinated_deadline_is_dropped(self, db, monkeypatch):
        """It would land on a compliance calendar. Anything that is not an
        unambiguous ISO date is discarded rather than coerced."""
        update = make_update(db, is_analysed=False)
        _model_returns(
            monkeypatch,
            {"summary": "Something changed.", "compliance_deadline": "end of next quarter"},
        )

        regulatory_tasks.analyse_update(db, update)

        assert update.compliance_deadline is None

    def test_an_iso_deadline_is_kept(self, db, monkeypatch):
        update = make_update(db, is_analysed=False)
        _model_returns(
            monkeypatch,
            {"summary": "Something changed.", "compliance_deadline": "2026-09-30"},
        )

        regulatory_tasks.analyse_update(db, update)

        assert update.compliance_deadline == date(2026, 9, 30)


class TestTheAnalysisSweep:
    """``analyse_pending_updates`` — twice daily, unattended.

    It analyses a batch and then fans each one out to tenants. The failure
    handling is the whole point of the loop: an update the model could not
    read must stay unanalysed so the next run retries it, and a crash on one
    circular must not cost the rest of the batch.
    """

    def test_it_analyses_and_fans_out(self, db, company, monkeypatch):
        make_update(db, is_analysed=False)
        db.commit()
        _model_returns(monkeypatch, {"summary": "The due date moved.", "impact_level": "high"})

        result = regulatory_tasks.analyse_pending_updates()

        assert result["analysed"] == 1
        assert result["impacts"] == 1
        db.expire_all()
        assert db.query(RegulatoryImpact).count() == 1

    def test_an_already_analysed_update_is_not_reanalysed(self, db, monkeypatch):
        """Every re-analysis is a paid model call over a document whose content
        has not changed."""
        make_update(db, is_analysed=True)
        db.commit()
        _model_returns(monkeypatch, {"summary": "Should not be called."})

        assert regulatory_tasks.analyse_pending_updates()["analysed"] == 0

    def test_a_soft_deleted_update_is_skipped(self, db, monkeypatch):
        update = make_update(db, is_analysed=False)
        update.soft_delete()
        db.commit()
        _model_returns(monkeypatch, {"summary": "Should not be called."})

        assert regulatory_tasks.analyse_pending_updates()["analysed"] == 0

    def test_the_batch_is_bounded(self, db, monkeypatch):
        for n in range(3):
            make_update(db, title=f"Circular {n}", is_analysed=False)
        db.commit()
        _model_returns(monkeypatch, {"summary": "Something changed."})

        assert regulatory_tasks.analyse_pending_updates(limit=2)["analysed"] == 2

    def test_the_newest_circular_is_analysed_first(self, db, monkeypatch):
        """A batch that cannot cover the backlog should spend itself on what
        people are asking about today."""
        old = make_update(db, title="Old", is_analysed=False, published_date=date(2026, 1, 1))
        new = make_update(db, title="New", is_analysed=False, published_date=date(2026, 8, 1))
        db.commit()
        _model_returns(monkeypatch, {"summary": "Something changed."})

        regulatory_tasks.analyse_pending_updates(limit=1)

        db.expire_all()
        assert db.get(RegulatoryUpdate, new.id).is_analysed is True
        assert db.get(RegulatoryUpdate, old.id).is_analysed is False

    def test_an_upstream_failure_leaves_the_update_for_the_next_run(
        self, db, monkeypatch
    ):
        """Marking it analysed with no analysis would bury the circular
        permanently — it would never appear in a backlog and never be mapped
        to anyone."""
        update = make_update(db, is_analysed=False)
        db.commit()
        monkeypatch.setattr(llm, "is_configured", lambda: True)

        def _down(*_a, **_k):
            raise llm.UpstreamError("502 from the provider")

        monkeypatch.setattr(llm, "complete_json", _down)

        result = regulatory_tasks.analyse_pending_updates()

        assert result["analysed"] == 0
        db.expire_all()
        assert db.get(RegulatoryUpdate, update.id).is_analysed is False

    def test_a_crash_on_one_circular_does_not_cost_the_batch(self, db, monkeypatch):
        doomed = make_update(db, title="Doomed", is_analysed=False)
        survivor = make_update(db, title="Survivor", is_analysed=False)
        db.commit()
        monkeypatch.setattr(llm, "is_configured", lambda: True)

        def _boom_on_doomed(prompt, **_k):
            if "Doomed" in prompt:
                raise RuntimeError("the analyser segfaulted")
            return _result({"summary": "Something changed."})

        monkeypatch.setattr(llm, "complete_json", _boom_on_doomed)

        result = regulatory_tasks.analyse_pending_updates()

        assert result["analysed"] == 1
        db.expire_all()
        assert db.get(RegulatoryUpdate, survivor.id).is_analysed is True
        assert db.get(RegulatoryUpdate, doomed.id).is_analysed is False

    def test_an_empty_backlog_is_not_an_error(self, db):
        assert regulatory_tasks.analyse_pending_updates() == {"analysed": 0, "impacts": 0}


class TestRemapUpdate:
    """The manual handle, for after an analysis is corrected by hand."""

    def test_it_fans_the_update_out_again(self, db, company):
        update = make_update(db)
        db.commit()

        result = regulatory_tasks.remap_update(update.id)

        assert result["matched"] == 1
        db.expire_all()
        assert db.query(RegulatoryImpact).count() == 1

    def test_a_corrected_analysis_reaches_the_tenants_it_now_matches(self, db, company):
        """The reason it exists. An officer widens an audience filter that was
        wrong, and the clients it should have reached get their alert without
        waiting for a re-ingest."""
        update = make_update(db, entity_types=["llp"])
        db.commit()
        regulatory_tasks.remap_update(update.id)
        assert db.query(RegulatoryImpact).count() == 0

        update.affected_entity_types_json = ["private_limited"]
        db.commit()

        assert regulatory_tasks.remap_update(update.id)["matched"] == 1

    def test_a_missing_update_is_reported_not_raised(self, db):
        assert regulatory_tasks.remap_update(9999) == {"update_id": 9999, "found": False}

    def test_one_broken_tenant_does_not_stop_the_fan_out(
        self, db, company, other_company, monkeypatch
    ):
        """A circular reaches four hundred clients or it reaches none.

        The fan-out commits per organization for exactly this reason: one
        tenant whose profile makes the assessment blow up costs that tenant
        their alert, not everybody else's.
        """
        update = make_update(db)
        db.commit()
        real = regulatory_tasks.assess_impact

        def _explode_on_first(session, org, upd):
            if org.id == company.id:
                raise RuntimeError("malformed profile")
            return real(session, org, upd)

        monkeypatch.setattr(regulatory_tasks, "assess_impact", _explode_on_first)

        result = regulatory_tasks.remap_update(update.id)

        assert result["matched"] == 1
        db.expire_all()
        assert db.query(RegulatoryImpact).filter_by(organization_id=company.id).count() == 0
        assert (
            db.query(RegulatoryImpact).filter_by(organization_id=other_company.id).count()
            == 1
        )

    def test_a_soft_deleted_update_is_not_remapped(self, db, company):
        update = make_update(db)
        update.soft_delete()
        db.commit()

        assert regulatory_tasks.remap_update(update.id)["found"] is False
        assert db.query(RegulatoryImpact).count() == 0


class TestAnalysisCoercion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("critical", ImpactLevel.CRITICAL),
            ("HIGH", ImpactLevel.HIGH),
            ("  low  ", ImpactLevel.LOW),
            ("nonsense", ImpactLevel.MEDIUM),
            (None, ImpactLevel.MEDIUM),
            (42, ImpactLevel.MEDIUM),
        ],
    )
    def test_impact_level_coercion_defaults_rather_than_raising(self, raw, expected):
        assert regulatory_tasks._coerce_level(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("gst", Regulation.GST), ("DPDP", Regulation.DPDP), ("bogus", None), (None, None)],
    )
    def test_regulation_coercion(self, raw, expected):
        assert regulatory_tasks._coerce_regulation(raw) == expected

    def test_a_valid_iso_date_parses(self):
        assert regulatory_tasks._parse_date("2026-09-30") == date(2026, 9, 30)

    @pytest.mark.parametrize(
        "raw", ["30 September 2026", "next quarter", "", None, "2026-13-45", 42]
    )
    def test_anything_ambiguous_is_dropped_rather_than_guessed(self, raw):
        """A hallucinated deadline lands on a compliance calendar."""
        assert regulatory_tasks._parse_date(raw) is None


class TestTenantIsolation:
    def test_one_tenants_impact_is_never_visible_to_another(
        self, db, company, other_company
    ):
        update = make_update(db)
        db.commit()
        regulatory_tasks.map_update_impacts(db, update)

        ours = db.query(RegulatoryImpact).filter_by(organization_id=company.id).all()
        theirs = db.query(RegulatoryImpact).filter_by(
            organization_id=other_company.id
        ).all()

        assert len(ours) == 1
        assert len(theirs) == 1
        assert ours[0].id != theirs[0].id

    def test_a_ca_firm_is_assessed_in_its_own_right(self, db, ca_firm):
        """A firm is an organization too; it files its own returns."""
        update = make_update(db, entity_types=["partnership"])
        db.commit()

        regulatory_tasks.map_update_impacts(db, update)

        assert (
            db.query(RegulatoryImpact).filter_by(organization_id=ca_firm.id).count() == 1
        )

    def test_an_update_filtered_to_companies_skips_a_ca_firm(self, db, ca_firm, company):
        update = make_update(db, entity_types=["private_limited"])
        db.commit()

        regulatory_tasks.map_update_impacts(db, update)

        assert db.query(RegulatoryImpact).filter_by(organization_id=ca_firm.id).count() == 0
        assert db.query(RegulatoryImpact).filter_by(organization_id=company.id).count() == 1


def test_org_type_fixture_sanity(db, ca_firm):
    """Guards the assumption the isolation tests above rest on."""
    assert ca_firm.type == OrgType.CA_FIRM
    assert ca_firm.entity_type == EntityType.PARTNERSHIP
