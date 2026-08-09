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
from app.tasks import regulatory_tasks
from tests.conftest import CRORE, make_org, make_user

TODAY = date(2026, 8, 8)


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
        published_date=TODAY,
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
