"""KPI, Pareto, trend, and sort-order calculations (PROJECT_SPEC.md sections 2 and 9).

Pure functions operating on plain numbers/dicts wherever possible, so both the API
layer and the unit tests can exercise the exact same math without a database.
"""

from __future__ import annotations

import datetime as dt
import decimal
from dataclasses import dataclass

PRIORITY_ORDER: list[str] = ["Urgent", "High", "Normal"]


def priority_sort_index(priority: str) -> int:
    try:
        return PRIORITY_ORDER.index(priority)
    except ValueError:
        return len(PRIORITY_ORDER)


def round_rate(value: float | None) -> float | None:
    """Round to 1 decimal for display while callers keep full precision internally."""
    if value is None:
        return None
    return round(value, 1)


@dataclass
class Kpis:
    drawers_inspected: int
    defect_events: int
    unique_drawers_rejected: int
    drawers_reworked: int
    defects_per_100: float | None
    rejection_rate: float | None
    first_pass_yield: float | None
    rework_rate: float | None
    internal_rework_cost: float
    total_internal_quality_cost: float
    quality_cost_per_drawer_inspected: float | None

    def to_dict(self) -> dict:
        return {
            "drawers_inspected": self.drawers_inspected,
            "defect_events": self.defect_events,
            "unique_drawers_rejected": self.unique_drawers_rejected,
            "drawers_reworked": self.drawers_reworked,
            "defects_per_100": round_rate(self.defects_per_100),
            "rejection_rate": round_rate(self.rejection_rate),
            "first_pass_yield": round_rate(self.first_pass_yield),
            "rework_rate": round_rate(self.rework_rate),
            "internal_rework_cost": round(self.internal_rework_cost, 2),
            "total_internal_quality_cost": round(self.total_internal_quality_cost, 2),
            "quality_cost_per_drawer_inspected": (
                round(self.quality_cost_per_drawer_inspected, 2)
                if self.quality_cost_per_drawer_inspected is not None
                else None
            ),
        }


def sum_internal_rework_cost(
    entries: list[tuple[int, decimal.Decimal | float | None]],
    *,
    fallback_rate: decimal.Decimal | float,
) -> float:
    """internal_rework_cost summed across a period's DailyProductionSummary rows
    (Phase 4 cost tracking; scrap cost was dropped from this app entirely - see
    docs/PROJECT_SPEC_PHASE4.md "Scrap removal").

    entries: (drawers_reworked, cost_per_drawer_at_time) per row. A row saved
    before Phase 4 existed has no rate snapshot (None) - it falls back to
    `fallback_rate` (normally today's configured rate) as the best available
    estimate, rather than silently costing it at $0.
    """
    rework_cost = 0.0
    for reworked, rate in entries:
        effective_rate = float(rate) if rate is not None else float(fallback_rate)
        rework_cost += reworked * effective_rate
    return rework_cost


def classify_case_cost_bucket(status: str, disposition: str | None) -> str | None:
    """Which cost bucket a defect case counts toward for the defect-case-derived
    cost fallback (see compute_internal_quality_cost / docs/PROJECT_SPEC_PHASE4.md
    "Defect-case fallback" section).

    A case already represents one defective drawer regardless of how many
    categories/items are on it (PROJECT_SPEC.md section 2), so the case itself -
    not its items - is the unit counted here. Still classifies "scrap" (the Scrap
    disposition/status are kept for backward compatibility - PROJECT_SPEC.md
    section 3.2), but only the "rework" bucket feeds any cost calculation now;
    callers that only care about rework cost can ignore a "scrap" result.
    """
    if status == "Closed - Scrapped" or disposition == "Scrap":
        return "scrap"
    if status == "Closed - Repaired" or disposition == "Rework":
        return "rework"
    return None


def defect_case_derived_rework_count(cases: list[tuple[dt.date, str, str | None]]) -> int:
    """Count of qualifying cases (one drawer per case) with disposition Rework
    closed as Closed - Repaired - the defect-case fallback source for internal
    rework cost when a production date has no DailyProductionSummary row at all.

    cases: (production_date, status, disposition) per case. production_date isn't
    used for the classification itself; callers pass it through because they've
    typically already filtered `cases` down to a specific date/bucket and this
    keeps the tuple shape self-describing.
    """
    return sum(
        1
        for _production_date, status, disposition in cases
        if classify_case_cost_bucket(status, disposition) == "rework"
    )


def compute_internal_quality_cost(
    *,
    daily_summary_entries: list[tuple[int, decimal.Decimal | float | None]],
    fallback_case_rework_count: int,
    fallback_rate: decimal.Decimal | float,
    has_daily_summary_rows: bool,
) -> dict:
    """Phase 4 fix: internal rework cost from BOTH sources, using the
    higher-resolution one for each production date. (Scrap cost was dropped from
    this app entirely - see docs/PROJECT_SPEC_PHASE4.md "Scrap removal" - so only
    rework is computed here.)

    `daily_summary_entries` covers dates that DO have a DailyProductionSummary row
    (same shape as sum_internal_rework_cost: (drawers_reworked,
    cost_per_drawer_at_time) per row) - that's the "official" count per
    PROJECT_SPEC_PHASE4.md, since it may include rework that never got a defect
    case. `fallback_case_rework_count` is derived from defect cases (via
    defect_case_derived_rework_count) whose production_date has NO daily summary
    row at all - this is what keeps cost from silently showing $0 when real defect
    cases exist but nobody filled out a Daily Production Summary for that date.
    Cases on a date that DOES have a summary are never added here, so the two
    sources are never double-counted for the same date. Case-derived cost always
    uses the *currently configured* rate, since a DefectCase has no rate snapshot of
    its own.

    Returns internal_rework_cost plus `cost_basis` ("daily_summary" |
    "defect_cases" | "blended" | "none") and the raw fallback count, so the UI can
    show which source is driving the number.
    """
    daily_rework_cost = sum_internal_rework_cost(daily_summary_entries, fallback_rate=fallback_rate)
    case_rework_cost = fallback_case_rework_count * float(fallback_rate)

    if fallback_case_rework_count == 0:
        cost_basis = "daily_summary" if has_daily_summary_rows else "none"
    elif not has_daily_summary_rows:
        cost_basis = "defect_cases"
    else:
        cost_basis = "blended"

    return {
        "internal_rework_cost": daily_rework_cost + case_rework_cost,
        "defect_case_rework_count": fallback_case_rework_count,
        "cost_basis": cost_basis,
    }


def compute_kpis(
    *,
    drawers_inspected: int,
    defect_events: int,
    unique_drawers_rejected: int,
    drawers_reworked: int,
    internal_rework_cost: float = 0.0,
) -> Kpis:
    """Implements PROJECT_SPEC.md section 2.1 exactly (minus Scrap Rate, dropped
    from this app - see docs/PROJECT_SPEC_PHASE4.md "Scrap removal"). Never divides
    by zero."""
    if drawers_inspected == 0:
        defects_per_100 = rejection_rate = first_pass_yield = rework_rate = None
    else:
        defects_per_100 = (defect_events / drawers_inspected) * 100
        rejection_rate = (unique_drawers_rejected / drawers_inspected) * 100
        first_pass_yield = ((drawers_inspected - unique_drawers_rejected) / drawers_inspected) * 100
        rework_rate = (drawers_reworked / drawers_inspected) * 100

    total_internal_quality_cost = internal_rework_cost
    quality_cost_per_drawer_inspected = (
        None if drawers_inspected == 0 else total_internal_quality_cost / drawers_inspected
    )

    return Kpis(
        drawers_inspected=drawers_inspected,
        defect_events=defect_events,
        unique_drawers_rejected=unique_drawers_rejected,
        drawers_reworked=drawers_reworked,
        defects_per_100=defects_per_100,
        rejection_rate=rejection_rate,
        first_pass_yield=first_pass_yield,
        rework_rate=rework_rate,
        internal_rework_cost=internal_rework_cost,
        total_internal_quality_cost=total_internal_quality_cost,
        quality_cost_per_drawer_inspected=quality_cost_per_drawer_inspected,
    )


def compute_resolved_on_the_spot_rate(
    *, total_cases: int, resolved_on_the_spot_count: int
) -> float | None:
    """PROJECT_SPEC.md section 3.3 KPI: % of (filtered) cases closed directly at
    entry via the "Fixed immediately?" fast path, instead of going through
    Open/In Rework/Ready for QC Recheck. Never divides by zero."""
    if total_cases == 0:
        return None
    return round_rate((resolved_on_the_spot_count / total_cases) * 100)


def compute_skip_recheck_rate(
    *, queued_rework_count: int, skipped_recheck_count: int
) -> float | None:
    """PROJECT_SPEC.md section 3.3 KPI: of the cases that actually reached In
    Rework (i.e. went through the queue rather than being resolved on the spot),
    what % closed directly via "Close Directly (Skip Recheck)" rather than going
    through Ready for QC Recheck. Never divides by zero."""
    if queued_rework_count == 0:
        return None
    return round_rate((skipped_recheck_count / queued_rework_count) * 100)


def filtered_defect_items_query(
    db,
    *,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    work_order_number: str | None = None,
    category_id: int | None = None,
    found_station_id: int | None = None,
    possible_source_station_id: int | None = None,
    priority: str | None = None,
    status: str | None = None,
    disposition: str | None = None,
):
    """Shared report filter set (PROJECT_SPEC.md section 9: 'chart totals must match
    the filtered record total'). Used by /reports/summary, /reports/pareto, and
    /reports/trend so they can never disagree with each other over what "filtered"
    means. Returns a query over (DefectItem, DefectCase) joined rows.
    """
    from app.models import DefectCase, DefectItem

    query = (
        db.query(DefectItem, DefectCase)
        .join(DefectCase, DefectItem.defect_case_id == DefectCase.id)
        .filter(DefectCase.is_deleted.is_(False))
    )

    if start_date is not None:
        query = query.filter(DefectCase.production_date >= start_date)
    if end_date is not None:
        query = query.filter(DefectCase.production_date <= end_date)
    if work_order_number:
        query = query.filter(DefectCase.work_order_number.ilike(f"%{work_order_number}%"))
    if category_id is not None:
        query = query.filter(DefectItem.defect_category_id == category_id)
    if found_station_id is not None:
        query = query.filter(DefectCase.found_station_id == found_station_id)
    if possible_source_station_id is not None:
        query = query.filter(DefectCase.possible_source_station_id == possible_source_station_id)
    if priority is not None:
        query = query.filter(DefectCase.priority == priority)
    if status is not None:
        query = query.filter(DefectCase.status == status)
    if disposition is not None:
        query = query.filter(DefectCase.disposition == disposition)

    return query


def trend_bucket_label(production_date: dt.date, group_by: str) -> str:
    """'day' -> ISO date string. 'week' -> ISO year-week, e.g. '2026-W30' (Mon-start)."""
    if group_by == "week":
        iso_year, iso_week, _ = production_date.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    return production_date.isoformat()


def compute_pareto(counts: dict[str, int]) -> list[dict]:
    """Sort a {label: defect_events} map highest-to-lowest with cumulative percentage.

    PROJECT_SPEC.md section 9: the default Pareto measure is Defect Events, sorted
    descending, with cumulative % = running total / grand total * 100.
    """
    total = sum(counts.values())
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    rows: list[dict] = []
    running = 0
    for label, count in ordered:
        running += count
        cumulative_pct = (running / total * 100) if total > 0 else None
        rows.append(
            {
                "label": label,
                "defect_events": count,
                "cumulative_pct": round_rate(cumulative_pct),
            }
        )
    return rows
