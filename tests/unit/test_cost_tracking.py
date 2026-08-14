"""Unit tests for Phase 4 internal cost tracking KPI formulas
(app/services/metrics_service.py)."""

from __future__ import annotations

import datetime as dt
import decimal

from app.services.metrics_service import (
    classify_case_cost_bucket,
    compute_internal_quality_cost,
    compute_kpis,
    defect_case_derived_rework_scrap_counts,
    sum_internal_quality_costs,
)


def test_sum_internal_quality_costs_uses_each_rows_own_rate():
    entries = [
        (5, 2, decimal.Decimal("35.00")),  # reworked=5, scrapped=2, rate=$35
        (3, 1, decimal.Decimal("50.00")),  # a later day at the new $50 rate
    ]
    rework_cost, scrap_cost = sum_internal_quality_costs(entries, fallback_rate=35.0)
    assert rework_cost == 5 * 35.0 + 3 * 50.0
    assert scrap_cost == 2 * 35.0 + 1 * 50.0


def test_sum_internal_quality_costs_falls_back_for_rows_with_no_snapshot():
    """A row saved before Phase 4 existed has cost_per_drawer_at_time = None."""
    entries = [(4, 0, None)]
    rework_cost, scrap_cost = sum_internal_quality_costs(entries, fallback_rate=40.0)
    assert rework_cost == 4 * 40.0
    assert scrap_cost == 0.0


def test_sum_internal_quality_costs_empty_entries_is_zero():
    rework_cost, scrap_cost = sum_internal_quality_costs([], fallback_rate=35.0)
    assert rework_cost == 0.0
    assert scrap_cost == 0.0


def test_compute_kpis_total_internal_quality_cost_and_per_drawer():
    kpis = compute_kpis(
        drawers_inspected=100,
        defect_events=10,
        unique_drawers_rejected=8,
        drawers_reworked=5,
        drawers_scrapped=2,
        internal_rework_cost=175.0,  # 5 * 35
        internal_scrap_cost=70.0,  # 2 * 35
    ).to_dict()
    assert kpis["internal_rework_cost"] == 175.0
    assert kpis["internal_scrap_cost"] == 70.0
    assert kpis["total_internal_quality_cost"] == 245.0
    assert kpis["quality_cost_per_drawer_inspected"] == 2.45  # 245 / 100


def test_compute_kpis_zero_inspected_returns_null_quality_cost_per_drawer():
    kpis = compute_kpis(
        drawers_inspected=0,
        defect_events=0,
        unique_drawers_rejected=0,
        drawers_reworked=0,
        drawers_scrapped=0,
        internal_rework_cost=0.0,
        internal_scrap_cost=0.0,
    ).to_dict()
    assert kpis["quality_cost_per_drawer_inspected"] is None
    # Total cost itself is still a real (zero) number, not null - there was simply
    # no cost, as opposed to "unknown because we can't divide by zero".
    assert kpis["total_internal_quality_cost"] == 0.0


def test_compute_kpis_defaults_cost_to_zero_when_not_provided():
    """Existing callers that don't pass cost params (pre-Phase-4 behavior) must
    still work, with cost reported as zero rather than raising."""
    kpis = compute_kpis(
        drawers_inspected=50,
        defect_events=5,
        unique_drawers_rejected=3,
        drawers_reworked=2,
        drawers_scrapped=1,
    ).to_dict()
    assert kpis["internal_rework_cost"] == 0.0
    assert kpis["internal_scrap_cost"] == 0.0
    assert kpis["total_internal_quality_cost"] == 0.0


# ---------------------------------------------------------------------------
# Phase 4 fix: dual-source internal cost (defect cases fall back for dates with
# no DailyProductionSummary row) - docs/PROJECT_SPEC_PHASE4.md.
# ---------------------------------------------------------------------------


def test_classify_case_cost_bucket_uses_disposition_when_not_yet_closed():
    assert classify_case_cost_bucket("In Rework", "Rework") == "rework"
    assert classify_case_cost_bucket("Waiting", "Hold") is None
    assert classify_case_cost_bucket("Open", None) is None


def test_classify_case_cost_bucket_uses_status_when_closed():
    assert classify_case_cost_bucket("Closed - Repaired", "Rework") == "rework"
    assert classify_case_cost_bucket("Closed - Scrapped", "Scrap") == "scrap"
    # A case that started life as a Rework disposition but was ultimately scrapped:
    # the final physical outcome (scrap) wins over the stale disposition.
    assert classify_case_cost_bucket("Closed - Scrapped", "Rework") == "scrap"


def test_defect_case_derived_rework_scrap_counts_counts_one_drawer_per_case():
    today = dt.date(2026, 7, 24)
    cases = [
        (today, "In Rework", "Rework"),
        (today, "Closed - Scrapped", "Scrap"),
        (today, "Open", None),  # not dispositioned yet - counts toward neither
        (today, "Waiting", "Hold"),  # Hold is neither rework nor scrap
    ]
    reworked, scrapped = defect_case_derived_rework_scrap_counts(cases)
    assert reworked == 1
    assert scrapped == 1


def test_compute_internal_quality_cost_pure_daily_summary():
    result = compute_internal_quality_cost(
        daily_summary_entries=[(5, 2, decimal.Decimal("35.00"))],
        fallback_case_counts=(0, 0),
        fallback_rate=35.0,
        has_daily_summary_rows=True,
    )
    assert result["internal_rework_cost"] == 175.0
    assert result["internal_scrap_cost"] == 70.0
    assert result["cost_basis"] == "daily_summary"
    assert result["defect_case_rework_count"] == 0
    assert result["defect_case_scrap_count"] == 0


def test_compute_internal_quality_cost_falls_back_to_defect_cases_when_no_summary():
    """The bug being fixed: defect cases with a Rework/Scrap disposition but no
    Daily Production Summary for that period must never show $0 cost."""
    result = compute_internal_quality_cost(
        daily_summary_entries=[],
        fallback_case_counts=(3, 1),
        fallback_rate=35.0,
        has_daily_summary_rows=False,
    )
    assert result["internal_rework_cost"] == 3 * 35.0
    assert result["internal_scrap_cost"] == 1 * 35.0
    assert result["cost_basis"] == "defect_cases"


def test_compute_internal_quality_cost_blends_both_sources():
    result = compute_internal_quality_cost(
        daily_summary_entries=[(5, 2, decimal.Decimal("35.00"))],
        fallback_case_counts=(1, 0),
        fallback_rate=35.0,
        has_daily_summary_rows=True,
    )
    assert result["internal_rework_cost"] == 5 * 35.0 + 1 * 35.0
    assert result["internal_scrap_cost"] == 2 * 35.0
    assert result["cost_basis"] == "blended"


def test_compute_internal_quality_cost_none_when_nothing_recorded():
    result = compute_internal_quality_cost(
        daily_summary_entries=[],
        fallback_case_counts=(0, 0),
        fallback_rate=35.0,
        has_daily_summary_rows=False,
    )
    assert result["internal_rework_cost"] == 0.0
    assert result["cost_basis"] == "none"
