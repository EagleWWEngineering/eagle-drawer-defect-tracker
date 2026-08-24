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
    # Phase 7 (PROJECT_SPEC_PHASE7.md): repurposed. No longer a sum of the (now
    # removed) Daily Production Summary "Drawers reworked" field - now the count
    # of distinct cases with disposition "Rework" in the filtered range, which is
    # also Rework Rate's new numerator. Field name kept for API/schema stability.
    drawers_reworked: int
    defects_per_100: float | None
    rejection_rate: float | None
    first_pass_yield: float | None
    rework_rate: float | None
    # Phase 7 cost model: sum of one cost unit per non-"Closed - Use As Is" case in
    # the filtered range (see compute_internal_quality_cost). Field name kept for
    # API/schema stability even though it's no longer specifically "rework" cost -
    # it's every case's cost except the ones that avoided it.
    internal_rework_cost: float
    # Phase 7, new: sum of the cost that WOULD have been incurred by every case in
    # the filtered range that instead closed "Closed - Use As Is" - the number
    # that makes "shipping as-is instead of reworking" visible as a saving.
    cost_avoided: float
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
            "cost_avoided": round(self.cost_avoided, 2),
            "total_internal_quality_cost": round(self.total_internal_quality_cost, 2),
            "quality_cost_per_drawer_inspected": (
                round(self.quality_cost_per_drawer_inspected, 2)
                if self.quality_cost_per_drawer_inspected is not None
                else None
            ),
        }


def compute_case_cost(
    *,
    status: str,
    cost_per_drawer_at_time: decimal.Decimal | float | None,
    fallback_rate: decimal.Decimal | float,
) -> float:
    """PROJECT_SPEC_PHASE7.md "Cost model": one defect case's contribution to
    Total Internal Quality Cost - one unit of its snapshotted
    cost_per_drawer_at_time, or `fallback_rate` (normally today's configured
    rate) for a case that predates this column, as the best available estimate
    rather than silently costing it at $0. Never multiplied by affected_drawer_
    quantity or DefectItem count - a case is one defective drawer no matter how
    many categories/items are on it (PROJECT_SPEC.md section 2).

    A case closed "Closed - Use As Is" contributes ZERO here - see
    compute_case_cost_avoided for its flip side.
    """
    if status == "Closed - Use As Is":
        return 0.0
    if cost_per_drawer_at_time is not None:
        return float(cost_per_drawer_at_time)
    return float(fallback_rate)


def compute_case_cost_avoided(
    *,
    status: str,
    cost_per_drawer_at_time: decimal.Decimal | float | None,
    fallback_rate: decimal.Decimal | float,
) -> float:
    """The flip side of compute_case_cost: the cost this case WOULD have counted
    as, had it not closed "Closed - Use As Is" - zero for every other status."""
    if status != "Closed - Use As Is":
        return 0.0
    if cost_per_drawer_at_time is not None:
        return float(cost_per_drawer_at_time)
    return float(fallback_rate)


def compute_internal_quality_cost(
    cases: list[tuple[str, decimal.Decimal | float | None]],
    *,
    fallback_rate: decimal.Decimal | float,
) -> dict:
    """PROJECT_SPEC_PHASE7.md "Cost model": Total Internal Quality Cost + Cost
    Avoided across `cases` - (status, cost_per_drawer_at_time snapshot) per
    distinct defect case in the filtered range. Replaces the old dual-source
    (Daily Production Summary + defect-case fallback) model from Phase 4 entirely
    - there is exactly one source now, and it is always the defect cases.
    """
    internal_rework_cost = sum(
        compute_case_cost(status=status, cost_per_drawer_at_time=rate, fallback_rate=fallback_rate)
        for status, rate in cases
    )
    cost_avoided = sum(
        compute_case_cost_avoided(
            status=status, cost_per_drawer_at_time=rate, fallback_rate=fallback_rate
        )
        for status, rate in cases
    )
    return {"internal_rework_cost": internal_rework_cost, "cost_avoided": cost_avoided}


def compute_kpis(
    *,
    drawers_inspected: int,
    defect_events: int,
    unique_drawers_rejected: int,
    drawers_reworked: int,
    internal_rework_cost: float = 0.0,
    cost_avoided: float = 0.0,
) -> Kpis:
    """Implements PROJECT_SPEC.md section 2.1, with PROJECT_SPEC_PHASE7.md's
    redefinition of Rework Rate: `drawers_reworked` here is the count of distinct
    cases with disposition "Rework" in the filtered range (see
    app/routers/reports.py get_summary), not a Daily Production Summary sum.
    Never divides by zero."""
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
        cost_avoided=cost_avoided,
        total_internal_quality_cost=total_internal_quality_cost,
        quality_cost_per_drawer_inspected=quality_cost_per_drawer_inspected,
    )


def compute_schedule_attainment_pct(
    *, total_inspected: int, total_scheduled: int | None
) -> float | None:
    """PROJECT_SPEC.md Phase 6 addendum 5b: total_inspected / total_scheduled * 100
    over a date range. None (-> "N/A" in the UI) when total_scheduled is 0 or
    unknown (None) - matching how compute_kpis already treats
    drawers_inspected == 0. A falsy total_scheduled (None or 0) is exactly the
    "unknown or zero" case, so one check covers both."""
    if not total_scheduled:
        return None
    return round_rate((total_inspected / total_scheduled) * 100)


def build_schedule_vs_completed(
    *,
    start_date: dt.date,
    end_date: dt.date,
    scheduled_by_date: dict[dt.date, int],
    inspected_by_date: dict[dt.date, int],
) -> dict:
    """Pure function behind the Dashboard's Scheduled vs Completed card
    (PROJECT_SPEC.md Phase 6 addendum 5b): one row per calendar day in
    [start_date, end_date] inclusive, pairing that day's known schedule (None if
    no daily_schedules row - never 0, a real "scheduled zero" fact must stay
    distinguishable) with drawers_inspected already summed across every shift
    that date (0 if no DailyProductionSummary rows exist for it - "the gap is
    exactly the signal Rodolfo wants to see"). Also returns the range totals and
    the Schedule Attainment % tile, computed from those same totals so the card
    and the tile can never disagree.

    total_scheduled is the sum of only the KNOWN days' figures - None if not a
    single day in the range has a daily_schedules row at all, distinct from a
    real total of 0.
    """
    days: list[dict] = []
    total_scheduled: int | None = None
    total_inspected = 0
    day = start_date
    while day <= end_date:
        scheduled = scheduled_by_date.get(day)
        inspected = inspected_by_date.get(day, 0)
        days.append(
            {"production_date": day, "drawers_scheduled": scheduled, "drawers_inspected": inspected}
        )
        if scheduled is not None:
            total_scheduled = (total_scheduled or 0) + scheduled
        total_inspected += inspected
        day += dt.timedelta(days=1)

    return {
        "days": days,
        "total_scheduled": total_scheduled,
        "total_inspected": total_inspected,
        "attainment_pct": compute_schedule_attainment_pct(
            total_inspected=total_inspected, total_scheduled=total_scheduled
        ),
    }


def compute_resolved_on_the_spot_rate(
    *, total_cases: int, resolved_on_the_spot_count: int
) -> float | None:
    """PROJECT_SPEC.md section 3.3 KPI: % of (filtered) cases closed directly at
    entry via the "Fixed immediately?" fast path, instead of going through
    Open/In Rework/Ready for QC Recheck. Never divides by zero."""
    if total_cases == 0:
        return None
    return round_rate((resolved_on_the_spot_count / total_cases) * 100)


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
