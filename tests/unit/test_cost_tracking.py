"""Unit tests for the PROJECT_SPEC_PHASE7.md "Cost model": one cost unit per
defect case (snapshotted at creation), zero for a case closed "Closed - Use As
Is" (which instead feeds Cost Avoided). Replaces the Phase 4 dual-source
(Daily Production Summary + defect-case fallback) model entirely.
"""

from __future__ import annotations

import decimal

from app.services.metrics_service import (
    compute_case_cost,
    compute_case_cost_avoided,
    compute_internal_quality_cost,
    compute_kpis,
)

# ---------------------------------------------------------------------------
# compute_case_cost / compute_case_cost_avoided: one unit per case
# ---------------------------------------------------------------------------


def test_case_cost_uses_its_own_snapshot():
    cost = compute_case_cost(
        status="Closed - Repaired",
        cost_per_drawer_at_time=decimal.Decimal("35.00"),
        fallback_rate=50.0,
    )
    assert cost == 35.0


def test_case_cost_falls_back_when_snapshot_is_null():
    """A case created before this column existed has no snapshot - falls back to
    the currently-configured rate, never silently $0."""
    cost = compute_case_cost(status="Open", cost_per_drawer_at_time=None, fallback_rate=40.0)
    assert cost == 40.0


def test_case_cost_is_zero_for_use_as_is():
    cost = compute_case_cost(
        status="Closed - Use As Is",
        cost_per_drawer_at_time=decimal.Decimal("35.00"),
        fallback_rate=35.0,
    )
    assert cost == 0.0


def test_case_cost_never_multiplied_by_quantity_or_item_count():
    """A case is one defective drawer no matter how many DefectItem
    categories/affected_drawer_quantity it carries - compute_case_cost has no
    such inputs at all, only status + rate."""
    cost = compute_case_cost(
        status="Open", cost_per_drawer_at_time=decimal.Decimal("35.00"), fallback_rate=35.0
    )
    assert cost == 35.0  # not 35 * N for any N


def test_open_case_counts_its_cost_immediately():
    """Open cases count their cost immediately - cost is removed only if/when
    the case closes "Closed - Use As Is"."""
    cost = compute_case_cost(
        status="Open", cost_per_drawer_at_time=decimal.Decimal("35.00"), fallback_rate=35.0
    )
    assert cost == 35.0


def test_historical_scrapped_case_counts_normally():
    """Closed - Scrapped counts as one unit like any other non-Use-As-Is case -
    Scrap stays removed from all tracking/reporting as a KPI, but a historical
    scrapped case is not specially zeroed out of cost."""
    cost = compute_case_cost(
        status="Closed - Scrapped",
        cost_per_drawer_at_time=decimal.Decimal("35.00"),
        fallback_rate=35.0,
    )
    assert cost == 35.0


def test_case_cost_avoided_is_zero_except_for_use_as_is():
    for status in ("Open", "Closed - Repaired", "Closed - Scrapped"):
        assert (
            compute_case_cost_avoided(
                status=status, cost_per_drawer_at_time=decimal.Decimal("35.00"), fallback_rate=35.0
            )
            == 0.0
        )


def test_case_cost_avoided_uses_the_same_rate_resolution_as_cost():
    avoided = compute_case_cost_avoided(
        status="Closed - Use As Is", cost_per_drawer_at_time=None, fallback_rate=42.0
    )
    assert avoided == 42.0


# ---------------------------------------------------------------------------
# compute_internal_quality_cost: totals across a filtered range of cases
# ---------------------------------------------------------------------------


def test_compute_internal_quality_cost_sums_one_unit_per_case():
    cases = [
        ("Open", decimal.Decimal("35.00")),
        ("Closed - Repaired", decimal.Decimal("35.00")),
        ("Closed - Repaired", decimal.Decimal("50.00")),  # a later day at the new rate
    ]
    result = compute_internal_quality_cost(cases, fallback_rate=35.0)
    assert result["internal_rework_cost"] == 35.0 + 35.0 + 50.0
    assert result["cost_avoided"] == 0.0


def test_compute_internal_quality_cost_use_as_is_moves_to_cost_avoided():
    cases = [
        ("Closed - Repaired", decimal.Decimal("35.00")),
        ("Closed - Use As Is", decimal.Decimal("35.00")),
        ("Closed - Use As Is", decimal.Decimal("35.00")),
    ]
    result = compute_internal_quality_cost(cases, fallback_rate=35.0)
    assert result["internal_rework_cost"] == 35.0
    assert result["cost_avoided"] == 70.0


def test_compute_internal_quality_cost_multi_category_case_still_one_unit():
    """A case with several DefectItem categories is still ONE case in this list -
    callers must dedupe to distinct cases before calling this (see
    app/routers/reports.py _distinct_cases); this function itself has no
    per-item concept at all, only one (status, rate) tuple per case."""
    cases = [("Closed - Repaired", decimal.Decimal("35.00"))]  # one case, however many items
    result = compute_internal_quality_cost(cases, fallback_rate=35.0)
    assert result["internal_rework_cost"] == 35.0


def test_compute_internal_quality_cost_empty_range_is_zero():
    result = compute_internal_quality_cost([], fallback_rate=35.0)
    assert result["internal_rework_cost"] == 0.0
    assert result["cost_avoided"] == 0.0


def test_compute_internal_quality_cost_null_snapshot_falls_back_to_current_rate():
    """A pre-migration case (null cost_per_drawer_at_time) uses the currently
    configured rate as its best-available estimate."""
    cases = [("Open", None), ("Open", None)]
    result = compute_internal_quality_cost(cases, fallback_rate=45.0)
    assert result["internal_rework_cost"] == 90.0


# ---------------------------------------------------------------------------
# compute_kpis: cost_avoided flows through to total_internal_quality_cost math
# ---------------------------------------------------------------------------


def test_compute_kpis_reports_cost_avoided_separately_from_total_cost():
    kpis = compute_kpis(
        drawers_inspected=100,
        defect_events=10,
        unique_drawers_rejected=8,
        drawers_reworked=5,
        internal_rework_cost=175.0,
        cost_avoided=70.0,
    ).to_dict()
    assert kpis["internal_rework_cost"] == 175.0
    assert kpis["cost_avoided"] == 70.0
    # total_internal_quality_cost is NOT reduced by cost_avoided - it's a
    # separate "what was saved" figure, not subtracted from the real cost.
    assert kpis["total_internal_quality_cost"] == 175.0
    assert kpis["quality_cost_per_drawer_inspected"] == 1.75


def test_compute_kpis_defaults_cost_avoided_to_zero_when_not_provided():
    kpis = compute_kpis(
        drawers_inspected=50,
        defect_events=5,
        unique_drawers_rejected=3,
        drawers_reworked=2,
        internal_rework_cost=70.0,
    ).to_dict()
    assert kpis["cost_avoided"] == 0.0


def test_compute_kpis_zero_inspected_returns_null_quality_cost_per_drawer():
    kpis = compute_kpis(
        drawers_inspected=0,
        defect_events=0,
        unique_drawers_rejected=0,
        drawers_reworked=0,
        internal_rework_cost=0.0,
    ).to_dict()
    assert kpis["quality_cost_per_drawer_inspected"] is None
    assert kpis["total_internal_quality_cost"] == 0.0


def test_compute_kpis_rework_rate_uses_case_count_not_a_daily_summary_sum():
    """PROJECT_SPEC_PHASE7.md: drawers_reworked passed to compute_kpis is now
    the count of cases with disposition "Rework" in the filtered range."""
    kpis = compute_kpis(
        drawers_inspected=100,
        defect_events=10,
        unique_drawers_rejected=8,
        drawers_reworked=4,  # 4 Rework-dispositioned cases, not a summary field
    ).to_dict()
    assert kpis["rework_rate"] == 4.0
