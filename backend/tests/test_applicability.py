"""The engine that decides which of ~200 obligations an organization owes.

Everything downstream is built on this answer. The filing generator iterates
it, the calendar renders what the generator produced, and the reminder sweep
chases what is on the calendar — so an obligation wrongly excluded here is not
a missing row on a screen, it is a return nobody was ever told to file.

Both directions of that error are expensive and neither is visible:

**A false negative is the penalty.** No filing generated looks exactly like no
filing owed, right up until the notice arrives. Nothing in the product says
"there was a rule you did not meet"; the obligation simply is not there.

**A false positive is the slower failure.** A five-person proprietorship shown
twelve returns it does not owe learns that the calendar is noise, and then
misses the one it did owe. That is why an unknown turnover resolves to *no*
rather than *yes*, and why the unknown is marked as a question rather than
buried in the same bucket as a settled no.

So the tests below are mostly a matrix over one rule at a time, holding the
rest of the profile clear. The interesting cases are the asymmetries: an unset
rule is not a constraint, a missing profile field is not a match, and a human's
override outranks the engine permanently rather than until the next sync.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.models.enums import EntityType, Frequency, Regulation
from app.models.obligation import OrganizationObligation
from app.services.applicability import (
    applicable_obligations,
    coverage_by_regulation,
    evaluate,
    sync_organization_obligations,
    visible_obligations,
)
from tests.conftest import CRORE, make_obligation, make_org

# A fixed reference date, so an obligation's effective window is decided by the
# test and not by the day CI happens to run.
TODAY = date(2026, 8, 8)


def verdict(db, org, **obligation_kwargs):
    """Evaluate one obligation against *org* on TODAY."""
    return evaluate(make_obligation(db, **obligation_kwargs), org, on=TODAY)


# The three boolean profile switches, paired with the catalogue rule that reads
# each one. They behave identically, so every test over them is parametrized.
FLAGS = [
    ("is_listed", "requires_listed"),
    ("has_foreign_investment", "requires_foreign_investment"),
    ("handles_personal_data", "requires_personal_data"),
]

_flag_org_counter = {"n": 0}


def org_with_flag(db, field: str, value: bool):
    """An organization with one profile switch set, and nothing else notable."""
    _flag_org_counter["n"] += 1
    org = make_org(db, name=f"Flag Co {_flag_org_counter['n']}")
    setattr(org, field, value)
    db.flush()
    return org


# --------------------------------------------------------------------------
# The default: an obligation with no rules
# --------------------------------------------------------------------------


class TestAnUnconstrainedObligation:
    def test_it_applies_to_everyone(self, db, company):
        """The catalogue's most common shape. GSTR-3B has no entity-type rule
        because every registered person files it."""
        assert verdict(db, company).applies

    def test_an_inactive_obligation_applies_to_nobody(self, db, company):
        """Retiring a catalogue entry has to take effect without editing every
        organization's record."""
        result = verdict(db, company, is_active=False)

        assert result.applies is False
        assert "no longer in the active catalogue" in result.reason

    def test_an_empty_rule_list_is_not_a_constraint(self, db, company):
        """``[]`` and ``None`` mean the same thing here, and the difference
        between them is not something a catalogue author should have to think
        about.

        Reading ``[]`` as "matches no entity type" would make a half-written
        entry apply to nobody — the failure mode this whole module is arranged
        to avoid, because it is invisible.
        """
        assert verdict(db, company, entity_types_json=[]).applies
        assert verdict(db, company, states_json=[]).applies
        assert verdict(db, company, industries_json=[]).applies


# --------------------------------------------------------------------------
# In-force window
# --------------------------------------------------------------------------


class TestInForce:
    def test_an_obligation_not_yet_in_force_does_not_apply(self, db, company):
        result = verdict(db, company, effective_from=date(2026, 10, 1))

        assert result.applies is False
        assert "2026-10-01" in result.reason

    def test_it_applies_on_the_day_it_comes_into_force(self, db, company):
        """Inclusive. A rule effective from the 8th is owed on the 8th, and an
        off-by-one here is a missed first period for every client at once."""
        assert verdict(db, company, effective_from=TODAY).applies

    def test_a_repealed_obligation_does_not_apply(self, db, company):
        result = verdict(db, company, effective_to=date(2026, 3, 31))

        assert result.applies is False
        assert "No longer in force" in result.reason

    def test_it_still_applies_on_its_final_day(self, db, company):
        assert verdict(db, company, effective_to=TODAY).applies

    def test_the_window_is_checked_before_anything_else(self, db, company):
        """A repealed return must not come back because the entity type still
        matches. The order of the checks is what guarantees that, so the reason
        is asserted rather than only the verdict."""
        result = verdict(
            db,
            company,
            effective_to=date(2026, 3, 31),
            entity_types_json=[str(EntityType.LLP)],
        )

        assert "No longer in force" in result.reason


# --------------------------------------------------------------------------
# Profile rules, one at a time
# --------------------------------------------------------------------------


class TestEntityType:
    def test_a_matching_entity_type_applies(self, db, company):
        assert verdict(
            db, company, entity_types_json=[str(EntityType.PRIVATE_LIMITED)]
        ).applies

    def test_a_non_matching_entity_type_does_not(self, db, company):
        """MGT-7 is for companies. Telling a proprietorship so is more useful
        than telling it about a turnover threshold it also did not meet."""
        result = verdict(db, company, entity_types_json=[str(EntityType.LLP)])

        assert result.applies is False
        assert str(EntityType.PRIVATE_LIMITED) in result.reason

    def test_an_unrecorded_entity_type_is_a_question_not_a_no(self, db):
        org = make_org(db, entity_type=None)

        result = verdict(db, org, entity_types_json=[str(EntityType.LLP)])

        assert result.applies is False
        assert result.uncertain is True
        assert result.missing_fields == ("entity_type",)


class TestState:
    def test_a_matching_state_applies(self, db, company):
        assert verdict(db, company, states_json=["Karnataka"]).applies

    def test_the_match_ignores_case_and_padding(self, db):
        """State names arrive from onboarding forms, CSV imports and the GST
        portal. "karnataka" from one of them is not a different state."""
        org = make_org(db, state="  karnataka ")

        assert verdict(db, org, states_json=["Karnataka"]).applies

    def test_another_state_does_not_apply(self, db, company):
        result = verdict(db, company, states_json=["Maharashtra"])

        assert result.applies is False
        assert "Karnataka" in result.reason

    def test_an_unrecorded_state_is_a_question(self, db):
        org = make_org(db, state=None)

        result = verdict(db, org, states_json=["Karnataka"])

        assert result.uncertain is True
        assert result.missing_fields == ("state",)


class TestIndustry:
    def test_a_matching_industry_applies(self, db):
        org = make_org(db, industry="Manufacturing")

        assert verdict(db, org, industries_json=["manufacturing"]).applies

    def test_another_industry_does_not(self, db):
        org = make_org(db, industry="Software")

        assert verdict(db, org, industries_json=["Manufacturing"]).applies is False

    def test_an_unrecorded_industry_is_a_question(self, db, company):
        result = verdict(db, company, industries_json=["Manufacturing"])

        assert result.uncertain is True
        assert result.missing_fields == ("industry",)


class TestTurnover:
    def test_turnover_above_the_floor_applies(self, db, company):
        assert verdict(db, company, min_turnover_paise=2 * CRORE).applies

    def test_turnover_below_the_floor_does_not(self, db, company):
        result = verdict(db, company, min_turnover_paise=10 * CRORE)

        assert result.applies is False
        assert "₹5.00 crore" in result.reason
        assert "₹10.00 crore" in result.reason

    def test_a_business_exactly_on_the_threshold_is_included(self, db, company):
        """Inclusive at both ends, deliberately.

        A regulator's "exceeding ₹5 crore" is written into the catalogue as ₹5
        crore and one paisa where the distinction matters. Reading the bound as
        exclusive here would silently drop every business sitting on a round
        number, which is a great many of them.
        """
        assert verdict(db, company, min_turnover_paise=5 * CRORE).applies
        assert verdict(db, company, max_turnover_paise=5 * CRORE).applies

    def test_turnover_above_the_ceiling_does_not_apply(self, db, company):
        """Composition-scheme obligations have an upper bound, so the ceiling
        is a real rule rather than a mirror of the floor."""
        result = verdict(db, company, max_turnover_paise=2 * CRORE)

        assert result.applies is False
        assert "above" in result.reason

    def test_a_band_excludes_from_both_sides(self, db, company):
        assert verdict(
            db, company, min_turnover_paise=1 * CRORE, max_turnover_paise=10 * CRORE
        ).applies
        assert not verdict(
            db, company, min_turnover_paise=6 * CRORE, max_turnover_paise=10 * CRORE
        ).applies

    def test_an_unrecorded_turnover_is_a_question(self, db):
        """The case the module docstring is about.

        Guessing yes floods a small business with returns it does not owe;
        guessing no is the penalty. Neither is a guess worth making, so the
        engine says which field would settle it.
        """
        org = make_org(db, annual_turnover_paise=None)

        result = verdict(db, org, min_turnover_paise=5 * CRORE)

        assert result.applies is False
        assert result.uncertain is True
        assert result.missing_fields == ("annual_turnover_paise",)

    def test_an_unrecorded_turnover_is_fine_when_there_is_no_threshold(self, db):
        org = make_org(db, annual_turnover_paise=None)

        assert verdict(db, org).applies


class TestEmployeeCount:
    def test_at_or_above_the_minimum_applies(self, db, company):
        """Ten-employee thresholds are how most labour obligations are worded,
        and the boundary is inclusive."""
        assert verdict(db, company, min_employees=25).applies
        assert verdict(db, company, min_employees=10).applies

    def test_below_the_minimum_does_not(self, db, company):
        result = verdict(db, company, min_employees=50)

        assert result.applies is False
        assert "25 employees" in result.reason

    def test_an_unrecorded_headcount_is_a_question(self, db):
        org = make_org(db, employee_count=None)

        result = verdict(db, org, min_employees=10)

        assert result.uncertain is True
        assert result.missing_fields == ("employee_count",)


class TestBooleanFlags:
    """The three switches that are never uncertain.

    Their columns are non-null with a default, so there is no "we do not know
    whether this company is listed" to represent — unlike turnover, which is
    genuinely absent until someone types it in. A flag rule therefore always
    produces a settled answer.
    """

    @pytest.mark.parametrize(("field", "rule"), FLAGS)
    def test_the_flag_must_match_when_the_rule_demands_it(self, db, field, rule):
        assert verdict(db, org_with_flag(db, field, True), **{rule: True}).applies
        assert not verdict(db, org_with_flag(db, field, False), **{rule: True}).applies

    @pytest.mark.parametrize(("field", "rule"), FLAGS)
    def test_a_rule_of_false_demands_the_flag_be_clear(self, db, field, rule):
        """Not the same as no rule at all.

        Some exemptions apply *only* to unlisted companies, so ``False`` has to
        mean "must not be set" rather than being folded into ``None``.
        """
        assert verdict(db, org_with_flag(db, field, False), **{rule: False}).applies
        assert not verdict(db, org_with_flag(db, field, True), **{rule: False}).applies

    @pytest.mark.parametrize("field", [field for field, _ in FLAGS])
    def test_no_rule_means_the_flag_is_not_consulted(self, db, field):
        assert verdict(db, org_with_flag(db, field, True)).applies
        assert verdict(db, org_with_flag(db, field, False)).applies

    def test_a_flag_verdict_is_never_uncertain(self, db, company):
        assert verdict(db, company, requires_listed=True).uncertain is False


# --------------------------------------------------------------------------
# Rules compose
# --------------------------------------------------------------------------


class TestRulesCompose:
    def test_every_rule_must_pass(self, db, company):
        """A conjunction, not a disjunction. Matching the entity type is not
        enough if the turnover floor is not met."""
        assert not verdict(
            db,
            company,
            entity_types_json=[str(EntityType.PRIVATE_LIMITED)],
            min_turnover_paise=10 * CRORE,
        ).applies

    def test_all_of_them_passing_applies(self, db, company):
        assert verdict(
            db,
            company,
            entity_types_json=[str(EntityType.PRIVATE_LIMITED)],
            states_json=["Karnataka"],
            min_turnover_paise=1 * CRORE,
            max_turnover_paise=10 * CRORE,
            min_employees=10,
        ).applies

    def test_the_most_specific_rejection_is_the_one_reported(self, db, company):
        """Two rules fail; the user sees the structural one.

        Telling a private limited company that an LLP-only form is for LLPs is
        actionable. Telling it the turnover threshold was not met invites
        someone to go and change the turnover.
        """
        result = verdict(
            db,
            company,
            entity_types_json=[str(EntityType.LLP)],
            min_turnover_paise=10 * CRORE,
        )

        assert str(EntityType.LLP) in result.reason
        assert "crore" not in result.reason


# --------------------------------------------------------------------------
# visible_obligations
# --------------------------------------------------------------------------


class TestVisibleObligations:
    def test_it_returns_the_system_catalogue(self, db, company):
        make_obligation(db)

        assert len(visible_obligations(db, company.id)) == 1

    def test_it_includes_the_tenants_own_custom_obligations(self, db, company):
        make_obligation(db, organization_id=company.id, code="acme.internal.review")

        codes = {o.code for o in visible_obligations(db, company.id)}
        assert "acme.internal.review" in codes

    def test_it_excludes_another_tenants_custom_obligations(self, db, company, other_company):
        make_obligation(db, organization_id=other_company.id, code="rival.secret.filing")

        codes = {o.code for o in visible_obligations(db, company.id)}
        assert "rival.secret.filing" not in codes

    def test_it_excludes_inactive_and_deleted_rows(self, db, company):
        make_obligation(db, is_active=False)
        deleted = make_obligation(db)
        deleted.soft_delete()
        db.flush()

        assert visible_obligations(db, company.id) == []


# --------------------------------------------------------------------------
# sync_organization_obligations
# --------------------------------------------------------------------------


class TestSync:
    def test_it_records_a_row_for_every_visible_obligation(self, db, company):
        make_obligation(db, min_turnover_paise=1 * CRORE)
        make_obligation(db, min_turnover_paise=99 * CRORE)

        result = sync_organization_obligations(db, company, on=TODAY)

        assert result.evaluated == 2
        assert result.created == 2

    def test_the_negatives_are_stored_too(self, db, company):
        """"GSTR-9 does not apply to you, because your turnover is under ₹2
        crore" is a thing a CA needs to be able to show a client. A table that
        held only the positives could not answer it."""
        make_obligation(db, min_turnover_paise=99 * CRORE)

        sync_organization_obligations(db, company, on=TODAY)

        row = db.query(OrganizationObligation).one()
        assert row.engine_verdict is False
        assert "below" in row.engine_reason

    def test_running_it_twice_creates_nothing_the_second_time(self, db, company):
        """It runs nightly and on every profile edit."""
        make_obligation(db)
        sync_organization_obligations(db, company, on=TODAY)

        second = sync_organization_obligations(db, company, on=TODAY)

        assert second.created == 0
        assert second.verdict_changed == 0
        assert db.query(OrganizationObligation).count() == 1

    def test_a_changed_profile_changes_the_verdict(self, db, company):
        obligation = make_obligation(db, min_turnover_paise=10 * CRORE)
        sync_organization_obligations(db, company, on=TODAY)

        company.annual_turnover_paise = 12 * CRORE
        db.flush()
        result = sync_organization_obligations(db, company, on=TODAY)

        assert result.verdict_changed == 1
        row = db.query(OrganizationObligation).filter_by(obligation_id=obligation.id).one()
        assert row.engine_verdict is True

    def test_a_changed_reason_alone_counts_as_a_change(self, db, company):
        """The reason is displayed, so a stale one is a wrong answer on screen
        even when the verdict it accompanies is still right."""
        make_obligation(db, min_turnover_paise=10 * CRORE)
        sync_organization_obligations(db, company, on=TODAY)

        company.annual_turnover_paise = 3 * CRORE
        db.flush()
        result = sync_organization_obligations(db, company, on=TODAY)

        assert result.verdict_changed == 1
        assert "₹3.00 crore" in db.query(OrganizationObligation).one().engine_reason

    def test_uncertain_verdicts_are_counted(self, db):
        org = make_org(db, annual_turnover_paise=None)
        make_obligation(db, min_turnover_paise=5 * CRORE)

        assert sync_organization_obligations(db, org, on=TODAY).uncertain == 1

    def test_it_can_be_limited_to_one_regulation(self, db, company):
        """A GST profile edit does not need to re-evaluate 200 rows."""
        make_obligation(db, regulation=Regulation.GST)
        make_obligation(db, regulation=Regulation.MCA)

        result = sync_organization_obligations(
            db, company, on=TODAY, regulations=[Regulation.GST]
        )

        assert result.evaluated == 1

    def test_it_does_not_touch_another_organizations_rows(self, db, company, other_company):
        make_obligation(db)
        sync_organization_obligations(db, other_company, on=TODAY)

        sync_organization_obligations(db, company, on=TODAY)

        owners = {r.organization_id for r in db.query(OrganizationObligation).all()}
        assert owners == {company.id, other_company.id}
        assert db.query(OrganizationObligation).count() == 2


class TestSyncNeverOverridesAHuman:
    """The property that makes the override worth having.

    A CA who marks a client exempt has almost certainly seen a fact the profile
    does not carry. A nightly re-sync that quietly undid that decision would be
    worse than no sync at all — the CA would have no way to make the exemption
    stick, and would eventually stop trusting the whole applicability record.
    """

    def test_the_override_survives_a_sync_that_disagrees(self, db, company):
        obligation = make_obligation(db)
        sync_organization_obligations(db, company, on=TODAY)
        row = db.query(OrganizationObligation).one()
        row.is_applicable_override = False
        db.flush()

        sync_organization_obligations(db, company, on=TODAY)

        db.refresh(row)
        assert row.is_applicable_override is False
        assert row.obligation_id == obligation.id

    def test_the_engines_own_opinion_is_still_recorded(self, db, company):
        """Both halves are kept, so the UI can say "we now think this applies;
        you have marked it exempt" rather than silently showing one of them."""
        make_obligation(db)
        sync_organization_obligations(db, company, on=TODAY)
        row = db.query(OrganizationObligation).one()
        row.is_applicable_override = False
        db.flush()

        sync_organization_obligations(db, company, on=TODAY)

        db.refresh(row)
        assert row.engine_verdict is True
        assert row.is_applicable is False

    def test_the_disagreement_is_surfaced_as_a_conflict(self, db, company):
        obligation = make_obligation(db)
        sync_organization_obligations(db, company, on=TODAY)
        db.query(OrganizationObligation).one().is_applicable_override = False
        db.flush()

        result = sync_organization_obligations(db, company, on=TODAY)

        assert result.conflicts == [obligation.id]

    def test_an_override_that_agrees_is_not_a_conflict(self, db, company):
        make_obligation(db)
        sync_organization_obligations(db, company, on=TODAY)
        db.query(OrganizationObligation).one().is_applicable_override = True
        db.flush()

        assert sync_organization_obligations(db, company, on=TODAY).conflicts == []


# --------------------------------------------------------------------------
# applicable_obligations — what the filing generator reads
# --------------------------------------------------------------------------


class TestApplicableObligations:
    def test_it_returns_what_the_engine_approved(self, db, company):
        make_obligation(db)
        sync_organization_obligations(db, company, on=TODAY)

        assert len(applicable_obligations(db, company.id)) == 1

    def test_it_omits_what_the_engine_rejected(self, db, company):
        make_obligation(db, min_turnover_paise=99 * CRORE)
        sync_organization_obligations(db, company, on=TODAY)

        assert applicable_obligations(db, company.id) == []

    def test_a_human_exemption_takes_effect_immediately(self, db, company):
        """It reads the *effective* verdict, not the engine's.

        A client marked exempt stops receiving filings on the next generation
        run rather than after the next sync — which matters, because the sync
        is nightly and the exemption is usually entered while someone is
        looking at the very calendar they want to stop.
        """
        make_obligation(db)
        sync_organization_obligations(db, company, on=TODAY)
        db.query(OrganizationObligation).one().is_applicable_override = False
        db.flush()

        assert applicable_obligations(db, company.id) == []

    def test_a_human_can_add_one_the_engine_rejected(self, db, company):
        make_obligation(db, min_turnover_paise=99 * CRORE)
        sync_organization_obligations(db, company, on=TODAY)
        db.query(OrganizationObligation).one().is_applicable_override = True
        db.flush()

        assert len(applicable_obligations(db, company.id)) == 1

    def test_an_obligation_retired_from_the_catalogue_drops_out(self, db, company):
        """Without touching the organization's row.

        The link stays — it is a record of what was once owed — but a retired
        obligation must stop producing filings the moment it is deactivated.
        """
        obligation = make_obligation(db)
        sync_organization_obligations(db, company, on=TODAY)
        obligation.is_active = False
        db.flush()

        assert applicable_obligations(db, company.id) == []

    def test_a_soft_deleted_link_drops_out(self, db, company):
        make_obligation(db)
        sync_organization_obligations(db, company, on=TODAY)
        db.query(OrganizationObligation).one().soft_delete()
        db.flush()

        assert applicable_obligations(db, company.id) == []

    def test_it_does_not_leak_another_tenants_obligations(
        self, db, company, other_company
    ):
        make_obligation(db)
        sync_organization_obligations(db, company, on=TODAY)
        sync_organization_obligations(db, other_company, on=TODAY)

        rows = applicable_obligations(db, company.id)

        assert {link.organization_id for link, _ in rows} == {company.id}


# --------------------------------------------------------------------------
# coverage_by_regulation
# --------------------------------------------------------------------------


class TestCoverageByRegulation:
    def test_it_splits_each_regulation_into_applies_and_does_not(self, db, company):
        make_obligation(db, regulation=Regulation.GST)
        make_obligation(db, regulation=Regulation.GST, min_turnover_paise=99 * CRORE)
        sync_organization_obligations(db, company, on=TODAY)

        summary = coverage_by_regulation(db, company.id)

        assert summary[str(Regulation.GST)]["applicable"] == 1
        assert summary[str(Regulation.GST)]["not_applicable"] == 1

    def test_a_regulation_with_nothing_evaluated_is_absent(self, db, company):
        make_obligation(db, regulation=Regulation.GST)
        sync_organization_obligations(db, company, on=TODAY)

        assert str(Regulation.SEBI) not in coverage_by_regulation(db, company.id)

    def test_an_override_is_counted_in_the_bucket_it_produces(self, db, company):
        """Not the engine's. The strip is a summary of what the client owes,
        and an obligation a CA has marked exempt is one they do not owe."""
        make_obligation(db, regulation=Regulation.GST)
        sync_organization_obligations(db, company, on=TODAY)
        db.query(OrganizationObligation).one().is_applicable_override = False
        db.flush()

        bucket = coverage_by_regulation(db, company.id)[str(Regulation.GST)]

        assert bucket == {"applicable": 0, "not_applicable": 1, "overridden": 1}

    def test_it_counts_rows_for_retired_obligations(self, db, company):
        """Unlike :func:`applicable_obligations`, which filters them out.

        The strip explains the applicability record, and a row that exists
        should be visible in the totals rather than vanishing without
        explanation.
        """
        obligation = make_obligation(db, regulation=Regulation.GST)
        sync_organization_obligations(db, company, on=TODAY)
        obligation.is_active = False
        db.flush()

        summary = coverage_by_regulation(db, company.id)

        assert summary[str(Regulation.GST)]["applicable"] == 1

    def test_it_is_scoped_to_one_organization(self, db, company, other_company):
        make_obligation(db, regulation=Regulation.GST)
        sync_organization_obligations(db, company, on=TODAY)
        sync_organization_obligations(db, other_company, on=TODAY)

        assert coverage_by_regulation(db, company.id)[str(Regulation.GST)][
            "applicable"
        ] == 1


# --------------------------------------------------------------------------
# Against the real catalogue
# --------------------------------------------------------------------------


class TestTheRealCatalogue:
    """One pass over the shipped ~200 obligations.

    The unit tests above use invented obligations, which proves the rules work
    but not that the catalogue exercises them sensibly. These check the shape
    of the answer for two profiles that must differ.
    """

    def test_a_small_proprietorship_owes_far_less_than_the_catalogue_holds(
        self, db, seeded
    ):
        org = make_org(
            db,
            name="Ravi Traders",
            entity_type=EntityType.PROPRIETORSHIP,
            annual_turnover_paise=CRORE // 2,
            employee_count=4,
        )

        result = sync_organization_obligations(db, org, on=TODAY)
        owed = applicable_obligations(db, org.id)

        assert result.evaluated > 50
        assert 0 < len(owed) < result.evaluated

    def test_a_listed_company_owes_more_than_an_unlisted_one(self, db, seeded):
        """SEBI's obligations exist and are gated on the listed flag, so this
        is the cheapest end-to-end check that a flag rule reaches the real
        catalogue at all."""
        unlisted = make_org(db, name="Private Co", is_listed=False)
        listed = make_org(db, name="Listed Co", is_listed=True)

        sync_organization_obligations(db, unlisted, on=TODAY)
        sync_organization_obligations(db, listed, on=TODAY)

        assert len(applicable_obligations(db, listed.id)) > len(
            applicable_obligations(db, unlisted.id)
        )

    def test_no_shipped_obligation_produces_an_empty_reason(self, db, seeded):
        """The reason is shown next to the obligation on a client's profile. A
        blank one is a bug the user sees before anyone else does."""
        org = make_org(db, name="Reason Check Ltd")
        sync_organization_obligations(db, org, on=TODAY)

        reasons = [r.engine_reason for r in db.query(OrganizationObligation).all()]
        assert reasons and all(r and r.strip() for r in reasons)

    def test_every_event_based_obligation_carries_an_offset(self, db, seeded):
        """The filing generator reads ``offset_days`` when an event is recorded
        and falls back to 30 if it is missing. A catalogue entry relying on
        that fallback would produce a plausible, wrong due date."""
        from app.models.obligation import ComplianceObligation

        event_based = (
            db.query(ComplianceObligation)
            .filter(ComplianceObligation.frequency == Frequency.EVENT_BASED)
            .all()
        )

        assert event_based
        assert all(o.offset_days is not None for o in event_based)
