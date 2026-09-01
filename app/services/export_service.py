"""CSV export (PROJECT_SPEC.md section 9: raw counts and identifiers, not just %)."""

from __future__ import annotations

import csv
import decimal
import io

from app.models import CustomerIssue, DefectCase, DefectItem
from app.services import metrics_service

CSV_COLUMNS = [
    "case_number",
    "production_date",
    "detected_at_utc",
    "work_order_number",
    # PROJECT_SPEC_PHASE9.md Part 2: its own column, never concatenated into
    # work_order_number - blank (not a dash) when this case has no line recorded.
    "line_label",
    "drawer_part_reference",
    "found_station",
    "possible_source_station",
    "priority",
    "status",
    "disposition",
    "defect_category",
    "affected_drawer_quantity",
    "repair_action",
    "root_cause",
    "corrective_action",
    "notes",
    # Phase 7 "Cost model": one cost unit per CASE (repeated on every item-line for
    # that case, same as production_date/work_order_number already are) - never
    # multiplied by affected_drawer_quantity. case_cost_per_drawer is the resolved
    # rate (this case's snapshot, or the fallback rate if it predates the
    # snapshot column) regardless of outcome; case_internal_cost is 0 for
    # "Closed - Use As Is" (that case's rate shows up in case_cost_avoided
    # instead). Replaces the old Phase 4 day_cost_per_drawer/day_internal_rework_
    # cost columns entirely - cost is per-case now, not per-date.
    "case_cost_per_drawer",
    "case_internal_cost",
    "case_cost_avoided",
    # Phase 6: same-day schedule context, joined by production_date. Blank (not
    # 0) if that date has no daily_schedules row - see
    # docs/PRODUCTION_BRIEF_SCHEDULE_SOURCE.md.
    "day_drawers_scheduled",
    "day_schedule_attainment_pct",
]


def build_defect_items_csv(
    rows: list[tuple[DefectItem, DefectCase]],
    *,
    fallback_rate: decimal.Decimal | float,
    daily_schedule_by_date: dict | None = None,
) -> str:
    """fallback_rate: the currently-configured cost_per_drawer rate, used for any
    case whose cost_per_drawer_at_time snapshot is null (it predates that column) -
    see app/services/metrics_service.py compute_case_cost.

    daily_schedule_by_date: production_date -> {"drawers_scheduled": int,
    "attainment_pct": float|None}, one entry per date that has a daily_schedules
    row. Missing for a date with no row at all - left blank, not zero, same rule.
    attainment_pct is itself None (blank) when that date's schedule is 0 (see
    metrics_service.compute_schedule_attainment_pct).
    """
    daily_schedule_by_date = daily_schedule_by_date or {}
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for item, case in rows:
        case_cost_per_drawer = (
            float(case.cost_per_drawer_at_time)
            if case.cost_per_drawer_at_time is not None
            else float(fallback_rate)
        )
        case_internal_cost = metrics_service.compute_case_cost(
            status=case.status,
            cost_per_drawer_at_time=case.cost_per_drawer_at_time,
            fallback_rate=fallback_rate,
        )
        case_cost_avoided = metrics_service.compute_case_cost_avoided(
            status=case.status,
            cost_per_drawer_at_time=case.cost_per_drawer_at_time,
            fallback_rate=fallback_rate,
        )

        day_schedule = daily_schedule_by_date.get(case.production_date)
        if day_schedule is not None:
            day_drawers_scheduled = str(day_schedule["drawers_scheduled"])
            attainment = day_schedule.get("attainment_pct")
            day_schedule_attainment_pct = str(attainment) if attainment is not None else ""
        else:
            day_drawers_scheduled = day_schedule_attainment_pct = ""

        writer.writerow(
            [
                case.case_number,
                case.production_date.isoformat(),
                case.detected_at.isoformat(),
                case.work_order_number,
                case.line_label or "",
                case.drawer_part_reference or "",
                case.found_station.name,
                case.possible_source_station.name if case.possible_source_station else "",
                case.priority,
                case.status,
                case.disposition or "",
                item.defect_category.name,
                item.affected_drawer_quantity,
                case.repair_action or "",
                case.root_cause or "",
                case.corrective_action or "",
                (case.notes or "").replace("\n", " "),
                case_cost_per_drawer,
                case_internal_cost,
                case_cost_avoided,
                day_drawers_scheduled,
                day_schedule_attainment_pct,
            ]
        )
    return buffer.getvalue()


CUSTOMER_ISSUE_CSV_COLUMNS = [
    "issue_number",
    "reported_date",
    "customer_name",
    "order_number",
    "issue_category",
    "source_type",
    "should_have_caught_at",
    "piece_count",
    "estimated_rework_cost",
    "status",
    "linked_defect_case_number",
    "description",
    "notes",
]


def build_customer_issues_csv(issues: list[CustomerIssue]) -> str:
    """Raw counts and identifiers, not just percentages (PROJECT_SPEC.md section 9,
    applied the same way to customer issues as to internal defect exports)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CUSTOMER_ISSUE_CSV_COLUMNS)
    for issue in issues:
        writer.writerow(
            [
                issue.issue_number,
                issue.reported_date.isoformat(),
                issue.customer_name,
                issue.order_number or "ORDER NOT IDENTIFIED",
                issue.issue_category.name,
                issue.source_type,
                issue.should_have_caught_at or "",
                issue.piece_count,
                str(issue.estimated_rework_cost) if issue.estimated_rework_cost is not None else "",
                issue.status,
                issue.linked_defect_case.case_number if issue.linked_defect_case else "",
                (issue.description or "").replace("\n", " "),
                (issue.notes or "").replace("\n", " "),
            ]
        )
    return buffer.getvalue()
