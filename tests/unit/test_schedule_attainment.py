"""Unit tests for the Phase 6 Schedule Attainment math in
app/services/metrics_service.py (PROJECT_SPEC.md Phase 6 addendum 5b).
"""

from __future__ import annotations

import datetime as dt

from app.services.metrics_service import (
    build_schedule_vs_completed,
    compute_schedule_attainment_pct,
)

# ---------------------------------------------------------------------------
# compute_schedule_attainment_pct
# ---------------------------------------------------------------------------


def test_attainment_normal_case():
    assert compute_schedule_attainment_pct(total_inspected=250, total_scheduled=400) == 62.5


def test_attainment_zero_scheduled_is_none():
    """0 scheduled -> N/A, never a divide-by-zero and never confused with 100%."""
    assert compute_schedule_attainment_pct(total_inspected=0, total_scheduled=0) is None
    assert compute_schedule_attainment_pct(total_inspected=50, total_scheduled=0) is None


def test_attainment_unknown_scheduled_is_none():
    """No daily_schedules row at all (None) -> N/A, same as 0 scheduled - both are
    "can't compute a meaningful rate", per compute_kpis's existing convention."""
    assert compute_schedule_attainment_pct(total_inspected=250, total_scheduled=None) is None


def test_attainment_over_100_when_more_completed_than_scheduled():
    assert compute_schedule_attainment_pct(total_inspected=500, total_scheduled=400) == 125.0


# ---------------------------------------------------------------------------
# build_schedule_vs_completed: gap-filling + range totals
# ---------------------------------------------------------------------------


# 2026-08-19/20/21 are Wed/Thu/Fri; 2026-08-22/23 are Sat/Sun - fixed dates used
# throughout so weekday/weekend status below is easy to eyeball.
WED, THU, FRI, SAT, SUN = (
    dt.date(2026, 8, 19),
    dt.date(2026, 8, 20),
    dt.date(2026, 8, 21),
    dt.date(2026, 8, 22),
    dt.date(2026, 8, 23),
)


def test_build_reports_gap_days_and_correct_totals():
    """2026-08-19: scheduled but no summary row at all (inspected defaults to 0).
    2026-08-20: has both. 2026-08-21: summary row but no schedule row (unknown,
    not 0). All three are working weekdays."""
    scheduled_by_date = {WED: 100, THU: 400}
    inspected_by_date = {THU: 250, FRI: 90}
    working_days = {WED, THU, FRI}

    result = build_schedule_vs_completed(
        start_date=WED,
        end_date=FRI,
        scheduled_by_date=scheduled_by_date,
        inspected_by_date=inspected_by_date,
        working_days=working_days,
    )

    days_by_date = {d["production_date"]: d for d in result["days"]}
    assert days_by_date[WED] == {
        "production_date": WED,
        "drawers_scheduled": 100,
        "drawers_inspected": 0,
        "is_working_day": True,
    }
    assert days_by_date[THU]["drawers_inspected"] == 250
    assert days_by_date[FRI]["drawers_scheduled"] is None
    assert days_by_date[FRI]["drawers_inspected"] == 90

    # total_scheduled sums only the KNOWN days (100 + 400), never treating the
    # unknown 8/21 as a zero.
    assert result["total_scheduled"] == 500
    assert result["total_inspected"] == 340
    assert result["attainment_pct"] == 68.0


def test_build_reports_none_total_scheduled_when_no_day_has_a_row():
    result = build_schedule_vs_completed(
        start_date=WED,
        end_date=THU,
        scheduled_by_date={},
        inspected_by_date={WED: 50},
        working_days={WED, THU},
    )
    assert result["total_scheduled"] is None
    assert result["attainment_pct"] is None
    assert result["total_inspected"] == 50


def test_build_reports_single_day_range():
    """Today/Yesterday presets: a single-day range still returns one day-pair
    plus correct totals."""
    result = build_schedule_vs_completed(
        start_date=THU,
        end_date=THU,
        scheduled_by_date={THU: 400},
        inspected_by_date={THU: 250},
        working_days={THU},
    )
    assert len(result["days"]) == 1
    assert result["total_scheduled"] == 400
    assert result["attainment_pct"] == 62.5


def test_build_reports_two_shift_day_is_not_double_counted_against_schedule():
    """A two-shift day's inspected total must already be summed across shifts by
    the caller (app/routers/daily_production.py) before reaching this function -
    this test locks in that the whole-day scheduled figure is compared against
    ONE combined inspected number, not per-shift."""
    scheduled_by_date = {THU: 400}
    # Caller-side sum of Day (150) + Night (100) shifts for the same date.
    inspected_by_date = {THU: 250}

    result = build_schedule_vs_completed(
        start_date=THU,
        end_date=THU,
        scheduled_by_date=scheduled_by_date,
        inspected_by_date=inspected_by_date,
        working_days={THU},
    )
    assert result["total_scheduled"] == 400
    assert result["total_inspected"] == 250
    assert result["attainment_pct"] == 62.5


# ---------------------------------------------------------------------------
# Working Days Logic (Part C addendum): weekend omission / holiday flagging
# ---------------------------------------------------------------------------


def test_build_reports_omits_a_weekend_with_no_data_silently():
    result = build_schedule_vs_completed(
        start_date=FRI,
        end_date=SUN,
        scheduled_by_date={FRI: 400},
        inspected_by_date={FRI: 380},
        working_days={FRI},
    )
    dates = [d["production_date"] for d in result["days"]]
    assert dates == [FRI]  # Sat/Sun dropped entirely, not just zeroed


def test_build_reports_keeps_an_overtime_saturday_as_a_working_day():
    result = build_schedule_vs_completed(
        start_date=FRI,
        end_date=SAT,
        scheduled_by_date={FRI: 400, SAT: 50},
        inspected_by_date={FRI: 380, SAT: 45},
        working_days={FRI, SAT},
    )
    days_by_date = {d["production_date"]: d for d in result["days"]}
    assert days_by_date[SAT]["is_working_day"] is True
    assert result["total_scheduled"] == 450
    assert result["total_inspected"] == 425


def test_build_reports_flags_a_holiday_weekday_but_does_not_drop_it():
    """Thursday was a holiday (scheduled 0, inspected 0 - so THU is not in
    working_days). It must stay in `days`, marked is_working_day=False, and
    contribute nothing to the totals."""
    result = build_schedule_vs_completed(
        start_date=WED,
        end_date=FRI,
        scheduled_by_date={WED: 100, THU: 0, FRI: 90},
        inspected_by_date={WED: 95, FRI: 85},
        working_days={WED, FRI},
    )
    dates = [d["production_date"] for d in result["days"]]
    assert dates == [WED, THU, FRI]  # the holiday stays, not dropped

    days_by_date = {d["production_date"]: d for d in result["days"]}
    assert days_by_date[THU]["is_working_day"] is False
    assert days_by_date[THU]["drawers_scheduled"] == 0

    # The holiday's 0/0 doesn't affect the totals either way, but is excluded
    # from the working-day set feeding attainment - not summed in.
    assert result["total_scheduled"] == 190
    assert result["total_inspected"] == 180
