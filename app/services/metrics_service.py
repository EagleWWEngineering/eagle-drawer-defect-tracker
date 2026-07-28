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
    drawers_scrapped: int
    defects_per_100: float | None
    rejection_rate: float | None
    first_pass_yield: float | None
    rework_rate: float | None
    scrap_rate: float | None
    internal_rework_cost: float
    internal_scrap_cost: float
    total_internal_quality_cost: float
    quality_cost_per_drawer_inspected: float | None

    def to_dict(self) -> dict:
        return {
            "drawers_inspected": self.drawers_inspected,
            "defect_events": self.defect_events,
            "unique_drawers_rejected": self.unique_drawers_rejected,
            "drawers_reworked": self.drawers_reworked,
            "drawers_scrapped": self.drawers_scrapped,
            "defects_per_100": round_rate(self.defects_per_100),
            "rejection_rate": round_rate(self.rejection_rate),
            "first_pass_yield": round_rate(self.first_pass_yield),
            "rework_rate": round_rate(self.rework_rate),
            "scrap_rate": round_rate(self.scrap_rate),
            "internal_rework_cost": round(self.internal_rework_cost, 2),
            "internal_scrap_cost": round(self.internal_scrap_cost, 2),
            "total_internal_quality_cost": round(self.total_internal_quality_cost, 2),
            "quality_cost_per_drawer_inspected": (
                round(self.quality_cost_per_drawer_inspected, 2)
                if self.quality_cost_per_drawer_inspected is not None
                else None
            ),
        }


def sum_internal_quality_costs(
    entries: list[tuple[int, int, decimal.Decimal | float | None]],
    *,
    fallback_rate: decimal.Decimal | float,
) -> tuple[float, float]:
    """(internal_rework_cost, internal_scrap_cost) summed across a period's
    DailyProductionSummary rows (Phase 4 cost tracking).

    entries: (drawers_reworked, drawers_scrapped, cost_per_drawer_at_time) per row.
    A row saved before this feature existed has no rate snapshot (None) - it falls
    back to `fallback_rate` (normally today's configured rate) as the best
    available estimate, rather than silently costing it at $0. See
    docs/PROJECT_SPEC_PHASE4.md.
    """
    rework_cost = 0.0
    scrap_cost = 0.0
    for reworked, scrapped, rate in entries:
        effective_rate = float(rate) if rate is not None else float(fallback_rate)
        rework_cost += reworked * effective_rate
        scrap_cost += scrapped * effective_rate
    return rework_cost, scrap_cost


def compute_kpis(
    *,
    drawers_inspected: int,
    defect_events: int,
    unique_drawers_rejected: int,
    drawers_reworked: int,
    drawers_scrapped: int,
    internal_rework_cost: float = 0.0,
    internal_scrap_cost: float = 0.0,
) -> Kpis:
    """Implements PROJECT_SPEC.md section 2.1 exactly. Never divides by zero."""
    if drawers_inspected == 0:
        defects_per_100 = rejection_rate = first_pass_yield = rework_rate = scrap_rate = None
    else:
        defects_per_100 = (defect_events / drawers_inspected) * 100
        rejection_rate = (unique_drawers_rejected / drawers_inspected) * 100
        first_pass_yield = ((drawers_inspected - unique_drawers_rejected) / drawers_inspected) * 100
        rework_rate = (drawers_reworked / drawers_inspected) * 100
        scrap_rate = (drawers_scrapped / drawers_inspected) * 100

    total_internal_quality_cost = internal_rework_cost + internal_scrap_cost
    quality_cost_per_drawer_inspected = (
        None if drawers_inspected == 0 else total_internal_quality_cost / drawers_inspected
    )

    return Kpis(
        drawers_inspected=drawers_inspected,
        defect_events=defect_events,
        unique_drawers_rejected=unique_drawers_rejected,
        drawers_reworked=drawers_reworked,
        drawers_scrapped=drawers_scrapped,
        defects_per_100=defects_per_100,
        rejection_rate=rejection_rate,
        first_pass_yield=first_pass_yield,
        rework_rate=rework_rate,
        scrap_rate=scrap_rate,
        internal_rework_cost=internal_rework_cost,
        internal_scrap_cost=internal_scrap_cost,
        total_internal_quality_cost=total_internal_quality_cost,
        quality_cost_per_drawer_inspected=quality_cost_per_drawer_inspected,
    )


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
