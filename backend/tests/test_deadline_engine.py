"""Period arithmetic and due-date calculation.

The module everything else is downstream of. A wrong due date here produces a
wrong reminder, a wrong calendar colour, and eventually a real penalty for a
real business — so the cases asserted are the actual statutory ones, named,
rather than synthetic inputs that only exercise the branches.

Every test passes an explicit date. A test that read the clock would pass in
July and fail in April, and the financial-year boundary is exactly where the
bugs live.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.models.enums import Frequency
from app.services.deadline_engine import (
    URGENCY_DEFAULT,
    DueDateRule,
    add_months,
    annual_period,
    due_date_for,
    event_due_date,
    event_period_key,
    financial_quarter,
    financial_year_label,
    financial_year_start,
    half_year_period,
    last_day_of_month,
    month_period,
    next_due_date,
    period_for,
    periods_between,
    quarter_period,
    urgency,
)


class TestFinancialYear:
    """April to March. The frame for every annual and quarterly obligation."""

    @pytest.mark.parametrize(
        "day,expected_start",
        [
            (date(2026, 4, 1), date(2026, 4, 1)),   # first day of FY2026-27
            (date(2026, 12, 31), date(2026, 4, 1)),
            (date(2027, 3, 31), date(2026, 4, 1)),  # last day of FY2026-27
            (date(2027, 4, 1), date(2027, 4, 1)),   # first day of FY2027-28
            (date(2026, 1, 15), date(2025, 4, 1)),  # January belongs to the prior FY
        ],
    )
    def test_year_starts_in_april(self, day, expected_start):
        assert financial_year_start(day) == expected_start

    def test_label_format(self):
        assert financial_year_label(date(2026, 4, 1)) == "FY2026-27"
        assert financial_year_label(date(2027, 3, 31)) == "FY2026-27"
        assert financial_year_label(date(2027, 4, 1)) == "FY2027-28"

    @pytest.mark.parametrize(
        "day,quarter",
        [
            (date(2026, 4, 1), 1),   # April-June is Q1, not Jan-Mar
            (date(2026, 6, 30), 1),
            (date(2026, 7, 1), 2),
            (date(2026, 10, 1), 3),
            (date(2027, 1, 1), 4),
            (date(2027, 3, 31), 4),
        ],
    )
    def test_quarters_run_from_april(self, day, quarter):
        assert financial_quarter(day) == quarter


class TestMonthArithmetic:
    def test_add_months_clamps_the_day_to_the_target_month(self):
        """31 January plus one month is 28 February, not an exception."""
        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
        assert add_months(date(2026, 1, 31), 3) == date(2026, 4, 30)

    def test_add_months_handles_a_leap_year(self):
        assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)

    def test_add_months_crosses_the_year(self):
        assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
        assert add_months(date(2026, 3, 15), -4) == date(2025, 11, 15)

    def test_last_day_of_month(self):
        assert last_day_of_month(2026, 2) == 28
        assert last_day_of_month(2028, 2) == 29
        assert last_day_of_month(2026, 12) == 31


class TestPeriods:
    def test_month_period(self):
        period = month_period(date(2026, 7, 15))
        assert period.key == "2026-07"
        assert period.start == date(2026, 7, 1)
        assert period.end == date(2026, 7, 31)

    def test_quarter_period_is_financial_not_calendar(self):
        period = quarter_period(date(2026, 8, 15))
        assert period.key == "FY2026-27-Q2"
        assert period.start == date(2026, 7, 1)
        assert period.end == date(2026, 9, 30)

    def test_quarter_period_at_the_year_boundary(self):
        period = quarter_period(date(2027, 2, 10))
        assert period.key == "FY2026-27-Q4"
        assert period.start == date(2027, 1, 1)
        assert period.end == date(2027, 3, 31)

    def test_half_year_period(self):
        first = half_year_period(date(2026, 5, 1))
        assert first.key == "FY2026-27-H1"
        assert (first.start, first.end) == (date(2026, 4, 1), date(2026, 9, 30))

        second = half_year_period(date(2026, 11, 1))
        assert second.key == "FY2026-27-H2"
        assert (second.start, second.end) == (date(2026, 10, 1), date(2027, 3, 31))

    def test_annual_period(self):
        period = annual_period(date(2026, 9, 1))
        assert period.key == "FY2026-27"
        assert (period.start, period.end) == (date(2026, 4, 1), date(2027, 3, 31))

    def test_non_periodic_frequencies_have_no_period(self):
        """Event-based obligations produce filings from events, not the calendar."""
        assert period_for(Frequency.EVENT_BASED, date(2026, 7, 1)) is None
        assert period_for(Frequency.ONE_TIME, date(2026, 7, 1)) is None

    def test_periods_between_enumerates_a_year_of_months(self):
        periods = periods_between(Frequency.MONTHLY, date(2026, 4, 1), date(2027, 3, 31))
        assert len(periods) == 12
        assert periods[0].key == "2026-04"
        assert periods[-1].key == "2027-03"

    def test_periods_between_enumerates_four_quarters(self):
        periods = periods_between(Frequency.QUARTERLY, date(2026, 4, 1), date(2027, 3, 31))
        assert [p.key for p in periods] == [
            "FY2026-27-Q1",
            "FY2026-27-Q2",
            "FY2026-27-Q3",
            "FY2026-27-Q4",
        ]

    def test_periods_between_is_empty_for_an_inverted_window(self):
        assert periods_between(Frequency.MONTHLY, date(2027, 1, 1), date(2026, 1, 1)) == []

    def test_periods_between_is_empty_for_a_non_periodic_frequency(self):
        assert periods_between(
            Frequency.EVENT_BASED, date(2026, 1, 1), date(2027, 1, 1)
        ) == []


class TestDueDates:
    """The three rules, against the returns they were written for."""

    def test_gstr3b_is_the_20th_of_the_following_month(self):
        rule = DueDateRule(frequency=Frequency.MONTHLY, due_day=20, period_offset=1)
        assert due_date_for(rule, month_period(date(2026, 7, 15))) == date(2026, 8, 20)

    def test_gstr3b_for_december_is_due_in_january(self):
        rule = DueDateRule(frequency=Frequency.MONTHLY, due_day=20, period_offset=1)
        assert due_date_for(rule, month_period(date(2026, 12, 5))) == date(2027, 1, 20)

    def test_a_due_day_past_the_month_end_is_clamped(self):
        """A 31st due day in a 30-day month is the 30th, not an error."""
        rule = DueDateRule(frequency=Frequency.MONTHLY, due_day=31, period_offset=1)
        # March's return is due in April, which has 30 days.
        assert due_date_for(rule, month_period(date(2026, 3, 1))) == date(2026, 4, 30)

    def test_offset_rule_counts_from_the_period_end(self):
        """SEBI shareholding pattern: 21 days after the quarter ends."""
        rule = DueDateRule(frequency=Frequency.QUARTERLY, offset_days=21)
        period = quarter_period(date(2026, 8, 1))  # ends 30 September
        assert due_date_for(rule, period) == date(2026, 10, 21)

    def test_fixed_month_rule_for_an_annual_return(self):
        """MGT-7: due 31 October of the year following the financial year."""
        rule = DueDateRule(
            frequency=Frequency.ANNUAL, due_month=10, due_day=31, period_offset=1
        )
        # FY2026-27 runs April 2026 to March 2027; the return is due Oct 2027.
        assert due_date_for(rule, annual_period(date(2026, 6, 1))) == date(2027, 10, 31)

    def test_fixed_month_before_april_rolls_into_the_next_calendar_year(self):
        """A January deadline for FY2026-27 is January 2028, not January 2027."""
        rule = DueDateRule(
            frequency=Frequency.ANNUAL, due_month=1, due_day=31, period_offset=1
        )
        assert due_date_for(rule, annual_period(date(2026, 6, 1))) == date(2028, 1, 31)

    def test_fixed_month_day_is_clamped_to_the_month(self):
        rule = DueDateRule(
            frequency=Frequency.ANNUAL, due_month=2, due_day=31, period_offset=1
        )
        assert due_date_for(rule, annual_period(date(2026, 6, 1))) == date(2028, 2, 29)

    def test_no_rule_falls_back_to_the_period_end(self):
        """Conservative: it can only ever be earlier than the truth."""
        rule = DueDateRule(frequency=Frequency.MONTHLY)
        assert due_date_for(rule, month_period(date(2026, 7, 1))) == date(2026, 7, 31)

    def test_quarterly_due_day_steps_a_whole_quarter(self):
        """A quarterly return's due day is in the month after the quarter ends."""
        rule = DueDateRule(frequency=Frequency.QUARTERLY, due_day=22, period_offset=1)
        period = quarter_period(date(2026, 5, 1))  # Q1: April-June
        assert due_date_for(rule, period) == date(2026, 7, 22)


class TestNextDueDate:
    def test_finds_the_next_upcoming_deadline(self):
        rule = DueDateRule(frequency=Frequency.MONTHLY, due_day=20, period_offset=1)
        # On 1 August, July's return (due 20 August) is still ahead.
        assert next_due_date(rule, today=date(2026, 8, 1)) == date(2026, 8, 20)

    def test_rolls_past_a_deadline_already_gone(self):
        rule = DueDateRule(frequency=Frequency.MONTHLY, due_day=20, period_offset=1)
        # On 21 August, July's has passed; August's is due 20 September.
        assert next_due_date(rule, today=date(2026, 8, 21)) == date(2026, 9, 20)

    def test_a_deadline_exactly_today_counts_as_next(self):
        rule = DueDateRule(frequency=Frequency.MONTHLY, due_day=20, period_offset=1)
        assert next_due_date(rule, today=date(2026, 8, 20)) == date(2026, 8, 20)

    def test_non_periodic_rule_has_no_next_date(self):
        rule = DueDateRule(frequency=Frequency.EVENT_BASED, offset_days=30)
        assert next_due_date(rule, today=date(2026, 8, 1)) is None


class TestEventBased:
    def test_fc_gpr_is_thirty_days_from_the_allotment(self):
        assert event_due_date(date(2026, 7, 15), 30) == date(2026, 8, 14)

    def test_period_key_is_the_event_date(self):
        """Two allotments on different days are two filings; one recorded twice is one."""
        assert event_period_key(date(2026, 7, 15)) == "2026-07-15"
        assert event_period_key(date(2026, 7, 15)) == event_period_key(date(2026, 7, 15))


class TestUrgency:
    @pytest.mark.parametrize(
        "days,band",
        [
            (-1, "overdue"),
            (-40, "overdue"),
            (0, "red"),
            (3, "red"),
            (7, "red"),      # boundaries are inclusive: exactly 7 days is red
            (8, "orange"),
            (15, "orange"),
            (16, "yellow"),
            (30, "yellow"),
            (31, URGENCY_DEFAULT),
            (365, URGENCY_DEFAULT),
        ],
    )
    def test_bands(self, days, band):
        assert urgency(days) == band
