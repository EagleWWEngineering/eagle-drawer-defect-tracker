"""CSV export (PROJECT_SPEC.md section 9: raw counts and identifiers, not just %)."""

from __future__ import annotations

import csv
import datetime as dt
import io

from app.models import CustomerIssue, DefectCase, DefectItem

CSV_COLUMNS = [
    "case_number",
    "production_date",
    "detected_at_utc",
    "work_order_number",
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
    # Phase 4: same-day cost context, joined by production_date. Blank if that
    # date has no Daily Production Summary saved. Scrap cost was dropped from this
    # app entirely (docs/PROJECT_SPEC_PHASE4.md "Scrap removal") - no scrap column.
    "day_cost_per_drawer",
    "day_internal_rework_cost",
]


def build_defect_items_csv(
    rows: list[tuple[DefectItem, DefectCase]],
    *,
    daily_cost_by_date: dict[dt.date, dict] | None = None,
) -> str:
    """daily_cost_by_date: production_date -> {"rate": Decimal|float, "rework_cost":
    float}, one entry per date (already summed across shifts if a date has more
    than one). Missing/empty for a date with no Daily Production Summary saved yet
    - those context columns are left blank, not zero.
    """
    daily_cost_by_date = daily_cost_by_date or {}
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for item, case in rows:
        day_cost = daily_cost_by_date.get(case.production_date)
        if day_cost is not None:
            day_cost_per_drawer = str(day_cost["rate"])
            day_internal_rework_cost = str(day_cost["rework_cost"])
        else:
            day_cost_per_drawer = day_internal_rework_cost = ""

        writer.writerow(
            [
                case.case_number,
                case.production_date.isoformat(),
                case.detected_at.isoformat(),
                case.work_order_number,
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
                day_cost_per_drawer,
                day_internal_rework_cost,
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
