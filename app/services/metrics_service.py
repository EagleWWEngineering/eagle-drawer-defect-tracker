"""KPI, Pareto, trend, and sort-order calculations (PROJECT_SPEC.md sections 2 and 9).

Pure functions operating on plain numbers/dicts wherever possible, so both the API
layer and the unit tests can exercise the exact same math without a database.
"""

from __future__ import annotations

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
        }


def compute_kpis(
    *,
    drawers_inspected: int,
    defect_events: int,
    unique_drawers_rejected: int,
    drawers_reworked: int,
    drawers_scrapped: int,
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
    )


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
