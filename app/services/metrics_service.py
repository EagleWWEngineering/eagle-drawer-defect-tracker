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


def classify_case_cost_bucket(status: str, disposition: str | None) -> str | None:
    """Which cost bucket a defect case counts toward for the defect-case-derived
    cost fallback (see compute_internal_quality_cost / docs/PROJECT_SPEC_PHASE4.md
    "Defect-case fallback" section).

    A case already represents one defective drawer regardless of how many
    categories/items are on it (PROJECT_SPEC.md section 2), so the case itself -
    not its items - is the unit counted here. Scrap wins over rework when both
    signals are present (e.g. a case originally dispositioned Rework that was
    later actually scrapped): the final physical outcome is what cost the shop
    money, not the interim intention.
    """
    if status == "Closed - Scrapped" or disposition == "Scrap":
        return "scrap"
    if status == "Closed - Repaired" or disposition == "Rework":
        return "rework"
    return None


def defect_case_derived_rework_scrap_counts(
    cases: list[tuple[dt.date, str, str | None]],
) -> tuple[int, int]:
    """(reworked_count, scrapped_count) - one drawer per qualifying case.

    cases: (production_date, status, disposition) per case. production_date isn't
    used for the classification itself; callers pass it through because they've
    typically already filtered `cases` down to a specific date/bucket and this
    keeps the tuple shape self-describing.
    """
    reworked = scrapped = 0
    for _production_date, status, disposition in cases:
        bucket = classify_case_cost_bucket(status, disposition)
        if bucket == "rework":
            reworked += 1
        elif bucket == "scrap":
            scrapped += 1
    return reworked, scrapped


def compute_internal_quality_cost(
    *,
    daily_summary_entries: list[tuple[int, int, decimal.Decimal | float | None]],
    fallback_case_counts: tuple[int, int],
    fallback_rate: decimal.Decimal | float,
    has_daily_summary_rows: bool,
) -> dict:
    """Phase 4 fix: internal rework/scrap cost from BOTH sources, using the
    higher-resolution one for each production date.

    `daily_summary_entries` covers dates that DO have a DailyProductionSummary row
    (same shape as sum_internal_quality_costs: (drawers_reworked, drawers_scrapped,
    cost_per_drawer_at_time) per row) - that's the "official" count per
    PROJECT_SPEC_PHASE4.md, since it may include rework/scrap that never got a
    defect case. `fallback_case_counts` is (reworked, scrapped) derived from defect
    cases (via defect_case_derived_rework_scrap_counts) whose production_date has NO
    daily summary row at all - this is what keeps cost from silently showing $0 when
    real defect cases exist but nobody filled out a Daily Production Summary for
    that date. Cases on a date that DOES have a summary are never added here, so the
    two sources are never double-counted for the same date. Case-derived cost always
    uses the *currently configured* rate, since a DefectCase has no rate snapshot of
    its own.

    Returns the two cost figures plus `cost_basis` ("daily_summary" | "defect_cases"
    | "blended" | "none") and the raw fallback counts, so the UI can show which
    source is driving the number.
    """
    daily_rework_cost, daily_scrap_cost = sum_internal_quality_costs(
        daily_summary_entries, fallback_rate=fallback_rate
    )
    case_reworked, case_scrapped = fallback_case_counts
    case_rework_cost = case_reworked * float(fallback_rate)
    case_scrap_cost = case_scrapped * float(fallback_rate)

    if case_reworked == 0 and case_scrapped == 0:
        cost_basis = "daily_summary" if has_daily_summary_rows else "none"
    elif not has_daily_summary_rows:
        cost_basis = "defect_cases"
    else:
        cost_basis = "blended"

    return {
        "internal_rework_cost": daily_rework_cost + case_rework_cost,
        "internal_scrap_cost": daily_scrap_cost + case_scrap_cost,
        "defect_case_rework_count": case_reworked,
        "defect_case_scrap_count": case_scrapped,
        "cost_basis": cost_basis,
    }


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
