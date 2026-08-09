"""Turning the obligations an organization owes into dated rows on a calendar.

The generator sits between the applicability engine, which knows *what* is
owed, and the deadline engine, which knows *when* a period's return is due.
Those two are tested elsewhere. What is tested here is the joining: which
periods land in the window, what happens when the same run happens twice, and
what the generator is forbidden from touching.

**Idempotence is the whole contract.** The nightly sweep overlaps with a manual
"refresh my calendar", with a retry after a worker died mid-body, and with
itself when two workers pick up the same organization. Running it twice must
not produce two GSTR-3Bs for July — a duplicate filing is not a cosmetic
problem, it is a second due date somebody chases, a second reminder thread, and
a client who stops believing the calendar.

**It only ever creates.** Work someone has started is never touched and a
period marked not applicable stays that way. The generator's job is to make
sure nothing is missing, not to make the calendar agree with its own idea of
the truth — a distinction that only shows up in the tests where a human has
already been at the row.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.enums import FilingStatus, Frequency, Regulation
from app.models.filing import Deadline, Filing
from app.models.obligation import OrganizationObligation
from app.services.applicability import sync_organization_obligations
from app.services.filing_generator import (
    DEFAULT_HORIZON_DAYS,
    DEFAULT_LOOKBACK_DAYS,
    generate_all,
    generate_for_organization,
    record_event_filing,
)
from tests.conftest import make_obligation

TODAY = date(2026, 8, 8)


def owed(db, org, **obligation_kwargs):
    """One catalogue obligation, synced so that *org* owes it.

    Goes through the applicability engine rather than inserting the link by
    hand: the generator reads the effective verdict, and a fixture that wrote
    ``engine_verdict`` directly would not prove the two agree on what that
    means.
    """
    obligation = make_obligation(db, **obligation_kwargs)
    sync_organization_obligations(db, org, on=TODAY)
    return obligation


def generate(db, org, **kwargs):
    kwargs.setdefault("today", TODAY)
    return generate_for_organization(db, org, **kwargs)


def filings(db, org):
    return db.query(Filing).filter(Filing.organization_id == org.id).all()


def period_keys(db, org) -> set[str]:
    return {f.period_key for f in filings(db, org)}


# --------------------------------------------------------------------------
# What gets generated
# --------------------------------------------------------------------------


class TestGeneration:
    def test_a_monthly_obligation_fills_the_window(self, db, company):
        """Roughly five months of GSTR-3B: 45 days back, 120 forward."""
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)

        result = generate(db, company)

        assert result.created == len(period_keys(db, company))
        assert result.created >= 4
        assert result.obligations_considered == 1

    def test_the_filing_carries_the_obligations_identity(self, db, company):
        obligation = owed(
            db,
            company,
            regulation=Regulation.GST,
            filing_type="GSTR-3B",
            frequency=Frequency.MONTHLY,
            due_day=20,
        )

        generate(db, company)

        filing = filings(db, company)[0]
        assert filing.obligation_id == obligation.id
        assert filing.regulation == Regulation.GST
        assert filing.filing_type == "GSTR-3B"
        assert filing.organization_id == company.id

    def test_a_generated_filing_starts_unworked(self, db, company):
        """Anything else would mean the sweep had formed an opinion about work
        nobody has done yet."""
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)

        generate(db, company)

        assert {f.status for f in filings(db, company)} == {FilingStatus.NOT_STARTED}

    def test_each_filing_records_the_period_it_covers(self, db, company):
        """The period bounds are what a return is prepared from — the month's
        invoices, not the month it is filed in."""
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)

        generate(db, company)

        for filing in filings(db, company):
            assert filing.period_start is not None
            assert filing.period_start <= filing.period_end
            assert filing.period_end < filing.due_date

    def test_every_filing_gets_its_deadline_row(self, db, company):
        """A filing with no deadline row is invisible to the reminder sweep —
        generated, on the calendar, and silently never chased."""
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)

        result = generate(db, company)

        assert db.query(Deadline).count() == result.created

    def test_a_quarterly_obligation_generates_fewer_than_a_monthly_one(
        self, db, company, other_company
    ):
        """The same window, at two cadences.

        Each obligation is owned by its own tenant, because a system obligation
        would be visible to both and the two counts would not be comparable.
        """
        make_obligation(
            db, organization_id=company.id, frequency=Frequency.MONTHLY, due_day=20
        )
        make_obligation(
            db,
            organization_id=other_company.id,
            frequency=Frequency.QUARTERLY,
            due_day=25,
        )
        sync_organization_obligations(db, company, on=TODAY)
        sync_organization_obligations(db, other_company, on=TODAY)

        monthly = generate(db, company)
        quarterly = generate(db, other_company)

        assert 0 < quarterly.created < monthly.created

    def test_an_organization_owing_nothing_gets_nothing(self, db, company):
        result = generate(db, company)

        assert result.created == 0
        assert result.obligations_considered == 0

    def test_an_obligation_the_engine_rejected_generates_nothing(self, db, company):
        """The distinction the result object exists to make: nothing was
        created because nothing was owed, not because everything existed."""
        owed(db, company, min_turnover_paise=99 * 100 * 100_000 * 100)

        result = generate(db, company)

        assert result.obligations_considered == 0
        assert result.created == 0


class TestTheWindow:
    def test_nothing_is_generated_beyond_the_horizon(self, db, company):
        """GST due dates are amended mid-year often enough that generating two
        years out would mean generating them wrong."""
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)

        generate(db, company)

        horizon = TODAY + timedelta(days=DEFAULT_HORIZON_DAYS)
        assert all(f.due_date <= horizon for f in filings(db, company))

    def test_nothing_is_generated_before_the_lookback(self, db, company):
        """Back-filling a year would present a new client with sixty overdue
        rows nobody intends to file."""
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)

        generate(db, company)

        floor = TODAY - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        assert all(f.due_date >= floor for f in filings(db, company))

    def test_the_recent_past_is_filled_in(self, db, company):
        """A client onboarded today needs last month's missed GSTR-3B to
        appear, so it can be dealt with rather than discovered."""
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)

        generate(db, company)

        assert any(f.due_date < TODAY for f in filings(db, company))

    def test_a_shorter_horizon_generates_less(self, db, company):
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)

        result = generate(db, company, horizon_days=20, lookback_days=0)

        assert result.created == 1

    def test_an_annual_return_due_inside_the_window_is_generated(self, db, company):
        """The reason periods are enumerated from a year further back than the
        window.

        An annual return's due date falls in the year *after* its period, so
        enumerating only periods inside the window would miss FY2025-26's
        return entirely while generating FY2026-27's, which is not yet due.
        """
        owed(db, company, frequency=Frequency.ANNUAL, due_month=10, due_day=31)

        generate(db, company, horizon_days=120)

        assert len(filings(db, company)) == 1
        filing = filings(db, company)[0]
        assert filing.due_date == date(2026, 10, 31)
        assert filing.period_start < date(2026, 4, 1)


class TestTheEffectiveWindowOfAnObligation:
    def test_a_period_that_ended_before_the_rule_existed_is_skipped(self, db, company):
        """An obligation that had not come into force when the period ended was
        not owed for that period, whatever today's date is."""
        owed(
            db,
            company,
            frequency=Frequency.MONTHLY,
            due_day=20,
            effective_from=date(2026, 8, 1),
        )

        generate(db, company)

        assert all(f.period_end >= date(2026, 8, 1) for f in filings(db, company))

    def test_a_period_starting_after_repeal_is_skipped(self, db, company):
        owed(
            db,
            company,
            frequency=Frequency.MONTHLY,
            due_day=20,
            effective_to=date(2026, 8, 31),
        )

        generate(db, company)

        assert all(f.period_start <= date(2026, 8, 31) for f in filings(db, company))

    def test_a_short_lived_rule_generates_only_its_own_periods(self, db, company):
        """Both bounds at once, on a rule in force for two months.

        June is excluded because the rule did not exist when the period ended;
        September because the rule was gone before the period began. Note that
        the rule must still be in force *today* or the applicability engine
        rejects it outright and the generator never sees it — which is why this
        window straddles TODAY rather than sitting in the past.
        """
        owed(
            db,
            company,
            frequency=Frequency.MONTHLY,
            due_day=20,
            effective_from=date(2026, 7, 1),
            effective_to=date(2026, 8, 31),
        )

        generate(db, company)

        assert period_keys(db, company) == {"2026-07", "2026-08"}

    def test_a_repealed_rule_is_not_generated_at_all(self, db, company):
        """The applicability engine rejects it before the generator runs, so a
        return retired last month does not keep appearing on the calendar for
        the periods it was once owed for."""
        owed(
            db,
            company,
            frequency=Frequency.MONTHLY,
            due_day=20,
            effective_to=date(2026, 7, 31),
        )

        assert generate(db, company).created == 0


# --------------------------------------------------------------------------
# Idempotence
# --------------------------------------------------------------------------


class TestIdempotence:
    def test_running_it_twice_creates_nothing_the_second_time(self, db, company):
        """The property the module is built around."""
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)
        first = generate(db, company)

        second = generate(db, company)

        assert second.created == 0
        assert second.skipped_existing == first.created
        assert len(filings(db, company)) == first.created

    def test_a_later_run_adds_only_the_period_that_came_into_range(self, db, company):
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)
        generate(db, company)

        result = generate(db, company, today=TODAY + timedelta(days=31))

        assert result.created == 1

    def test_work_already_started_is_left_alone(self, db, company):
        """The generator only ever creates. A filing somebody is halfway
        through is not a row for a sweep to have opinions about."""
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)
        generate(db, company)
        filing = filings(db, company)[0]
        filing.status = FilingStatus.DRAFT
        filing.data_json = {"turnover": 100}
        db.flush()

        generate(db, company)

        db.refresh(filing)
        assert filing.status == FilingStatus.DRAFT
        assert filing.data_json == {"turnover": 100}

    def test_a_voided_filing_is_not_quietly_recreated(self, db, company):
        """Soft-deleted periods count as present.

        Otherwise deleting a filing would be impossible to sustain — the next
        nightly sweep would put it straight back, and the partial unique index
        would let the insert through because it excludes deleted rows.
        """
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)
        generate(db, company)
        voided = filings(db, company)[0]
        voided.soft_delete()
        db.flush()

        result = generate(db, company)

        assert result.created == 0
        assert len(db.query(Filing).filter(Filing.period_key == voided.period_key).all()) == 1

    def test_an_exemption_entered_today_stops_tomorrows_generation(self, db, company):
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)
        db.query(OrganizationObligation).one().is_applicable_override = False
        db.flush()

        assert generate(db, company).created == 0

    def test_a_lost_insert_race_is_counted_not_raised(self, db, company, monkeypatch):
        """Two workers both looked, both saw nothing, and both inserted.

        The unique index catches it. What matters is that the collision rolls
        back only the one INSERT: without the savepoint, a conflict partway
        through an organization would abort the transaction and lose every
        filing created before it.
        """
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)
        calls = {"n": 0}
        real_flush = db.flush

        def _collide_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("uq_filing_org_obligation_period", None, Exception())
            return real_flush(*args, **kwargs)

        monkeypatch.setattr(db, "flush", _collide_once)
        result = generate(db, company)
        monkeypatch.undo()

        assert result.skipped_conflict == 1
        assert result.created >= 1
        assert len(filings(db, company)) == result.created


# --------------------------------------------------------------------------
# Obligations with no period
# --------------------------------------------------------------------------


class TestObligationsWithoutPeriods:
    @pytest.mark.parametrize("frequency", [Frequency.EVENT_BASED, Frequency.ONE_TIME])
    def test_they_are_not_generated_from_the_calendar(self, db, company, frequency):
        """FC-GPR is due 30 days after an allotment, and there is no allotment
        until someone records one. Generating it from the calendar would put it
        on every client's dashboard whether or not one ever happened."""
        owed(db, company, frequency=frequency, offset_days=30, due_day=None)

        result = generate(db, company)

        assert result.obligations_considered == 1
        assert result.created == 0

    def test_recording_the_event_creates_the_filing(self, db, company):
        obligation = owed(
            db, company, frequency=Frequency.EVENT_BASED, offset_days=30, due_day=None
        )

        filing = record_event_filing(
            db, company, obligation, event_date=date(2026, 7, 15)
        )

        assert filing is not None
        assert filing.due_date == date(2026, 8, 14)
        assert filing.period_start == date(2026, 7, 15)
        assert filing.period_end == date(2026, 7, 15)

    def test_the_offset_comes_from_the_obligation(self, db, company):
        obligation = owed(
            db, company, frequency=Frequency.EVENT_BASED, offset_days=180, due_day=None
        )

        filing = record_event_filing(
            db, company, obligation, event_date=date(2026, 7, 15)
        )

        assert filing is not None
        assert filing.due_date == date(2027, 1, 11)

    def test_recording_the_same_event_twice_is_harmless(self, db, company):
        """A user who clicks twice, or a form resubmitted after a timeout."""
        obligation = owed(
            db, company, frequency=Frequency.EVENT_BASED, offset_days=30, due_day=None
        )
        record_event_filing(db, company, obligation, event_date=date(2026, 7, 15))

        again = record_event_filing(
            db, company, obligation, event_date=date(2026, 7, 15)
        )

        assert again is None
        assert len(filings(db, company)) == 1

    def test_two_events_on_different_days_are_two_filings(self, db, company):
        """Two allotments in the same month are two FC-GPRs, so the period key
        has to be the event date rather than the month."""
        obligation = owed(
            db, company, frequency=Frequency.EVENT_BASED, offset_days=30, due_day=None
        )

        record_event_filing(db, company, obligation, event_date=date(2026, 7, 15))
        record_event_filing(db, company, obligation, event_date=date(2026, 7, 22))

        assert len(filings(db, company)) == 2

    def test_the_event_payload_is_stored_on_the_filing(self, db, company):
        obligation = owed(
            db, company, frequency=Frequency.EVENT_BASED, offset_days=30, due_day=None
        )

        filing = record_event_filing(
            db,
            company,
            obligation,
            event_date=date(2026, 7, 15),
            data={"shares_allotted": 5000},
        )

        assert filing is not None
        assert filing.data_json == {"shares_allotted": 5000}

    def test_an_event_filing_gets_a_deadline_row(self, db, company):
        obligation = owed(
            db, company, frequency=Frequency.EVENT_BASED, offset_days=30, due_day=None
        )

        record_event_filing(db, company, obligation, event_date=date(2026, 7, 15))

        assert db.query(Deadline).count() == 1

    def test_a_later_sweep_does_not_duplicate_it(self, db, company):
        obligation = owed(
            db, company, frequency=Frequency.EVENT_BASED, offset_days=30, due_day=None
        )
        record_event_filing(db, company, obligation, event_date=date(2026, 7, 15))

        generate(db, company)

        assert len(filings(db, company)) == 1


# --------------------------------------------------------------------------
# Per-organization overrides
# --------------------------------------------------------------------------


class TestPerOrganizationOverrides:
    def test_a_due_day_override_moves_the_dates(self, db, company):
        """A client with an extension, or a firm that wants its own internal
        deadline ahead of the statutory one."""
        owed(db, company, frequency=Frequency.MONTHLY, due_day=20)
        db.query(OrganizationObligation).one().due_day_override = 11
        db.flush()

        generate(db, company)

        assert all(f.due_date.day == 11 for f in filings(db, company))

    def test_a_frequency_override_changes_the_cadence(self, db, company):
        """A QRMP taxpayer files GSTR-3B quarterly. Same obligation, different
        cadence — which is a property of the client, not the catalogue."""
        owed(db, company, frequency=Frequency.MONTHLY, due_day=22)
        db.query(OrganizationObligation).one().frequency_override = Frequency.QUARTERLY
        db.flush()

        generate(db, company)

        assert len(filings(db, company)) < 4


# --------------------------------------------------------------------------
# generate_all — the nightly sweep's entry point
# --------------------------------------------------------------------------


class TestGenerateAll:
    def test_it_covers_every_active_organization(self, db, company, other_company):
        make_obligation(db, frequency=Frequency.MONTHLY, due_day=20)
        sync_organization_obligations(db, company, on=TODAY)
        sync_organization_obligations(db, other_company, on=TODAY)
        db.commit()

        results = generate_all(db, today=TODAY)

        assert {r.organization_id for r in results} == {company.id, other_company.id}
        assert all(r.created > 0 for r in results)

    def test_it_can_be_pointed_at_a_subset(self, db, company, other_company):
        make_obligation(db, frequency=Frequency.MONTHLY, due_day=20)
        sync_organization_obligations(db, company, on=TODAY)
        sync_organization_obligations(db, other_company, on=TODAY)
        db.commit()

        results = generate_all(db, today=TODAY, organization_ids=[other_company.id])

        assert [r.organization_id for r in results] == [other_company.id]

    def test_a_deactivated_organization_is_skipped(self, db, company, other_company):
        """A cancelled account stops accruing a calendar. Not soft-deleted —
        just switched off, which is the state a lapsed subscription is in.

        The active neighbour is asserted present as well, so that a sweep which
        had stopped generating for *everybody* could not pass this.
        """
        make_obligation(db, frequency=Frequency.MONTHLY, due_day=20)
        sync_organization_obligations(db, company, on=TODAY)
        sync_organization_obligations(db, other_company, on=TODAY)
        company.is_active = False
        db.commit()

        results = generate_all(db, today=TODAY)

        assert {r.organization_id for r in results} == {other_company.id}

    def test_a_soft_deleted_organization_is_skipped(self, db, company):
        make_obligation(db, frequency=Frequency.MONTHLY, due_day=20)
        sync_organization_obligations(db, company, on=TODAY)
        company.soft_delete()
        db.commit()

        assert generate_all(db, today=TODAY) == []

    def test_each_organization_is_committed_as_it_finishes(self, db, company):
        """Not one transaction over four hundred clients.

        A sweep that held everything open until the end would roll the whole
        night back on the last tenant's bad data, which is a sweep that never
        succeeds.
        """
        make_obligation(db, frequency=Frequency.MONTHLY, due_day=20)
        sync_organization_obligations(db, company, on=TODAY)
        db.commit()

        generate_all(db, today=TODAY)

        db.rollback()
        assert filings(db, company) != []

    def test_one_broken_tenant_does_not_stop_the_others(
        self, db, company, other_company, monkeypatch
    ):
        """One client's malformed profile costs that client's iteration.

        The alternative is a sweep whose failure mode is that nobody's calendar
        is generated, discovered the following morning by four hundred people
        at once.
        """
        make_obligation(db, frequency=Frequency.MONTHLY, due_day=20)
        sync_organization_obligations(db, company, on=TODAY)
        sync_organization_obligations(db, other_company, on=TODAY)
        db.commit()

        import app.services.filing_generator as module

        real = module.generate_for_organization

        def _explode_on_first(session, org, **kwargs):
            if org.id == company.id:
                raise RuntimeError("malformed profile")
            return real(session, org, **kwargs)

        monkeypatch.setattr(module, "generate_for_organization", _explode_on_first)

        results = generate_all(db, today=TODAY)

        assert [r.organization_id for r in results] == [other_company.id]
        assert filings(db, other_company) != []

    def test_the_survivors_work_is_still_committed(self, db, company, other_company, monkeypatch):
        """The rollback after a failure must undo the failed tenant only."""
        make_obligation(db, frequency=Frequency.MONTHLY, due_day=20)
        sync_organization_obligations(db, company, on=TODAY)
        sync_organization_obligations(db, other_company, on=TODAY)
        db.commit()

        import app.services.filing_generator as module

        real = module.generate_for_organization

        def _explode_on_first(session, org, **kwargs):
            if org.id == company.id:
                raise RuntimeError("malformed profile")
            return real(session, org, **kwargs)

        monkeypatch.setattr(module, "generate_for_organization", _explode_on_first)
        generate_all(db, today=TODAY)
        monkeypatch.undo()

        db.rollback()
        assert filings(db, company) == []
        assert filings(db, other_company) != []
