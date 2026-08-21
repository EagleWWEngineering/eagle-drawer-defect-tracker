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


def test_build_reports_gap_days_and_correct_totals():
    """2026-08-19: scheduled but no summary row at all (inspected defaults to 0).
    2026-08-20: has both. 2026-08-21: summary row but no schedule row (unknown,
    not 0)."""
    scheduled_by_date = {dt.date(2026, 8, 19): 100, dt.date(2026, 8, 20): 400}
    inspected_by_date = {dt.date(2026, 8, 20): 250, dt.date(2026, 8, 21): 90}

    result = build_schedule_vs_completed(
        start_date=dt.date(2026, 8, 19),
        end_date=dt.date(2026, 8, 21),
        scheduled_by_date=scheduled_by_date,
        inspected_by_date=inspected_by_date,
    )

    days_by_date = {d["production_date"]: d for d in result["days"]}
    assert days_by_date[dt.date(2026, 8, 19)] == {
        "production_date": dt.date(2026, 8, 19),
        "drawers_scheduled": 100,
        "drawers_inspected": 0,
    }
    assert days_by_date[dt.date(2026, 8, 20)]["drawers_inspected"] == 250
    assert days_by_date[dt.date(2026, 8, 21)]["drawers_scheduled"] is None
    assert days_by_date[dt.date(2026, 8, 21)]["drawers_inspected"] == 90

    # total_scheduled sums only the KNOWN days (100 + 400), never treating the
    # unknown 8/21 as a zero.
    assert result["total_scheduled"] == 500
    assert result["total_inspected"] == 340
    assert result["attainment_pct"] == 68.0


def test_build_reports_none_total_scheduled_when_no_day_has_a_row():
    result = build_schedule_vs_completed(
        start_date=dt.date(2026, 8, 19),
        end_date=dt.date(2026, 8, 20),
        scheduled_by_date={},
        inspected_by_date={dt.date(2026, 8, 19): 50},
    )
    assert result["total_scheduled"] is None
    assert result["attainment_pct"] is None
    assert result["total_inspected"] == 50


def test_build_reports_single_day_range():
    """Today/Yesterday presets: a single-day range still returns one day-pair
    plus correct totals."""
    result = build_schedule_vs_completed(
        start_date=dt.date(2026, 8, 20),
        end_date=dt.date(2026, 8, 20),
        scheduled_by_date={dt.date(2026, 8, 20): 400},
        inspected_by_date={dt.date(2026, 8, 20): 250},
    )
    assert len(result["days"]) == 1
    assert result["total_scheduled"] == 400
    assert result["attainment_pct"] == 62.5


def test_build_reports_two_shift_day_is_not_double_counted_against_schedule():
    """A two-shift day's inspected total must already be summed across shifts by
    the caller (app/routers/daily_production.py) before reaching this function -
    this test locks in that the whole-day scheduled figure is compared against
    ONE combined inspected number, not per-shift."""
    scheduled_by_date = {dt.date(2026, 8, 20): 400}
    # Caller-side sum of Day (150) + Night (100) shifts for the same date.
    inspected_by_date = {dt.date(2026, 8, 20): 250}

    result = build_schedule_vs_completed(
        start_date=dt.date(2026, 8, 20),
        end_date=dt.date(2026, 8, 20),
        scheduled_by_date=scheduled_by_date,
        inspected_by_date=inspected_by_date,
    )
    assert result["total_scheduled"] == 400
    assert result["total_inspected"] == 250
    assert result["attainment_pct"] == 62.5
