"""Dashboard KPIs, Pareto, trend, work-order drilldown, and the rework queue.

All counting math is delegated to app/services/metrics_service.py so the numbers
shown here can never drift from the numbers in exports or MCP tool results.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, selectinload

from app.dependencies import get_db
from app.models import DailyProductionSummary, DefectCase, DefectItem
from app.schemas import (
    DatePresetOut,
    KpiOut,
    ParetoRowOut,
    ReworkQueueItemOut,
    TrendPointOut,
    WorkOrderHistoryOut,
    WorkOrderLineBreakdownOut,
    defect_case_to_out,
)
from app.services import metrics_service, schedule_service, settings_service, working_days_service
from app.services.defect_service import DIRECT_CLOSE_SOURCE_STATUSES
from app.timezone_utils import resolve_date_preset, today_in_display_timezone

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])
rework_router = APIRouter(prefix="/api/v1", tags=["rework-queue"])


@router.get("/date-preset", response_model=DatePresetOut)
def get_date_preset(preset: str, db: Session = Depends(get_db)) -> DatePresetOut:
    """Resolve one of the Dashboard's date-range preset buttons (Today/Yesterday/
    Last 7 working days/Last 30 working days/Month to date) to concrete
    {start_date, end_date}, in DISPLAY_TIMEZONE. The dashboard calls this rather
    than computing the boundary itself in JavaScript, so there is exactly one
    implementation of "what does 'Yesterday' mean" to get right and test.

    Working Days Logic (Part C addendum): "Yesterday"/"Last 7 days"/"Last 30
    days" are working-day-aware (a Monday's "Yesterday" is Friday, not Sunday) -
    those go through working_days_service.resolve_working_day_preset(), the only
    place in this router that needs a DB session for date-preset resolution.
    "Today"/"Month to date" stay purely calendar-based via
    app/timezone_utils.py resolve_date_preset()."""
    if preset in working_days_service.WORKING_DAY_PRESETS:
        today = today_in_display_timezone()
        start_date, end_date = working_days_service.resolve_working_day_preset(
            db, preset, today=today
        )
    else:
        start_date, end_date = resolve_date_preset(preset)
    return DatePresetOut(start_date=start_date, end_date=end_date)


def _daily_summary_rows(
    db: Session, start_date: dt.date | None, end_date: dt.date | None
) -> list[DailyProductionSummary]:
    query = db.query(DailyProductionSummary)
    if start_date is not None:
        query = query.filter(DailyProductionSummary.production_date >= start_date)
    if end_date is not None:
        query = query.filter(DailyProductionSummary.production_date <= end_date)
    return query.all()


def _daily_totals(rows: list[DailyProductionSummary]) -> dict:
    return {
        "drawers_inspected": sum(r.drawers_inspected for r in rows),
        "drawers_rejected_unique": sum(r.drawers_rejected_unique for r in rows),
    }


def _distinct_cases(items: list[tuple[DefectItem, DefectCase]]) -> list[DefectCase]:
    """De-dupe a (DefectItem, DefectCase) result set down to its distinct cases -
    a case with N items appears N times in `items`."""
    cases_by_id: dict[int, DefectCase] = {}
    for _item, case in items:
        cases_by_id[case.id] = case
    return list(cases_by_id.values())


@router.get("/summary", response_model=KpiOut)
def get_summary(
    db: Session = Depends(get_db),
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    work_order_number: str | None = None,
    line_label: str | None = None,
    category_id: int | None = None,
    found_station_id: int | None = None,
    possible_source_station_id: int | None = None,
    priority: str | None = None,
    status: str | None = None,
    disposition: str | None = None,
) -> KpiOut:
    items_query = metrics_service.filtered_defect_items_query(
        db,
        start_date=start_date,
        end_date=end_date,
        work_order_number=work_order_number,
        line_label=line_label,
        category_id=category_id,
        found_station_id=found_station_id,
        possible_source_station_id=possible_source_station_id,
        priority=priority,
        status=status,
        disposition=disposition,
    )
    items = items_query.all()
    defect_events = sum(item.affected_drawer_quantity for item, _case in items)
    totals = _daily_totals(_daily_summary_rows(db, start_date, end_date))
    cases = _distinct_cases(items)

    # PROJECT_SPEC_PHASE7.md "Cost model": one cost unit per case in the filtered
    # range (zero for "Closed - Use As Is", which instead feeds Cost Avoided) -
    # the only cost source now, replacing Phase 4's dual daily-summary/defect-case
    # model entirely.
    fallback_rate = settings_service.get_cost_per_drawer(db)
    cost_result = metrics_service.compute_internal_quality_cost(
        [(c.status, c.cost_per_drawer_at_time) for c in cases], fallback_rate=fallback_rate
    )

    # PROJECT_SPEC_PHASE7.md: Rework Rate's numerator is now the count of cases
    # with disposition "Rework" in the filtered range, full stop - no status
    # qualifier, and no more reading DailyProductionSummary.drawers_reworked.
    rework_case_count = sum(1 for c in cases if c.disposition == "Rework")

    kpis = metrics_service.compute_kpis(
        drawers_inspected=totals["drawers_inspected"],
        defect_events=defect_events,
        unique_drawers_rejected=totals["drawers_rejected_unique"],
        drawers_reworked=rework_case_count,
        internal_rework_cost=cost_result["internal_rework_cost"],
        cost_avoided=cost_result["cost_avoided"],
    ).to_dict()

    # PROJECT_SPEC.md section 3.3 KPI (60-second-fix fast path) - definition
    # unchanged by Phase 7.
    total_cases = len(cases)
    resolved_on_the_spot_count = sum(1 for c in cases if c.resolved_on_the_spot)
    kpis["total_cases"] = total_cases
    kpis["resolved_on_the_spot_count"] = resolved_on_the_spot_count
    kpis["pct_resolved_on_the_spot"] = metrics_service.compute_resolved_on_the_spot_rate(
        total_cases=total_cases, resolved_on_the_spot_count=resolved_on_the_spot_count
    )
    return KpiOut(**kpis)


@router.get("/pareto", response_model=list[ParetoRowOut])
def get_pareto(
    db: Session = Depends(get_db),
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    work_order_number: str | None = None,
    line_label: str | None = None,
    found_station_id: int | None = None,
    possible_source_station_id: int | None = None,
    priority: str | None = None,
    status: str | None = None,
    disposition: str | None = None,
    group_by: str = "category",
    limit: int = 10,
) -> list[ParetoRowOut]:
    """group_by: 'category' (default) or 'source_station'.

    Possible source station is a hypothesis, not a confirmed root cause — the label
    returned for that grouping is "possible source station", never "root cause".
    """
    items_query = metrics_service.filtered_defect_items_query(
        db,
        start_date=start_date,
        end_date=end_date,
        work_order_number=work_order_number,
        line_label=line_label,
        found_station_id=found_station_id,
        possible_source_station_id=possible_source_station_id,
        priority=priority,
        status=status,
        disposition=disposition,
    )

    counts: dict[str, int] = {}
    for item, case in items_query.all():
        if group_by == "source_station":
            label = case.possible_source_station.name if case.possible_source_station else "Unknown"
        else:
            label = item.defect_category.name
        counts[label] = counts.get(label, 0) + item.affected_drawer_quantity

    rows = metrics_service.compute_pareto(counts)
    return [ParetoRowOut(**r) for r in rows[:limit]]


@router.get("/trend", response_model=list[TrendPointOut])
def get_trend(
    db: Session = Depends(get_db),
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    group_by: str = "day",
) -> list[TrendPointOut]:
    items_query = metrics_service.filtered_defect_items_query(
        db, start_date=start_date, end_date=end_date
    )
    items = items_query.all()
    events_by_bucket: dict[str, int] = {}
    for item, case in items:
        label = metrics_service.trend_bucket_label(case.production_date, group_by)
        events_by_bucket[label] = events_by_bucket.get(label, 0) + item.affected_drawer_quantity

    summary_rows = _daily_summary_rows(db, start_date, end_date)
    fallback_rate = settings_service.get_cost_per_drawer(db)

    inspected_by_bucket: dict[str, int] = {}
    rejected_by_bucket: dict[str, int] = {}
    for row in summary_rows:
        label = metrics_service.trend_bucket_label(row.production_date, group_by)
        inspected_by_bucket[label] = inspected_by_bucket.get(label, 0) + row.drawers_inspected
        rejected_by_bucket[label] = rejected_by_bucket.get(label, 0) + row.drawers_rejected_unique

    # PROJECT_SPEC_PHASE7.md "Cost model": one cost unit per case, bucketed the
    # same way as every other per-date rollup here - replaces the old Phase 4
    # dual-source (daily-summary + defect-case-fallback) model entirely.
    rework_cost_by_bucket: dict[str, float] = {}
    cost_avoided_by_bucket: dict[str, float] = {}
    for case in _distinct_cases(items):
        label = metrics_service.trend_bucket_label(case.production_date, group_by)
        cost_result = metrics_service.compute_internal_quality_cost(
            [(case.status, case.cost_per_drawer_at_time)], fallback_rate=fallback_rate
        )
        rework_cost_by_bucket[label] = (
            rework_cost_by_bucket.get(label, 0.0) + cost_result["internal_rework_cost"]
        )
        cost_avoided_by_bucket[label] = (
            cost_avoided_by_bucket.get(label, 0.0) + cost_result["cost_avoided"]
        )

    # Phase 6: Scheduled + Schedule Attainment % per bucket, alongside the other
    # per-date rollups above - same bucketing helper, so a "week" grouping sums
    # schedule the same way it sums everything else.
    scheduled_by_bucket: dict[str, int] = {}
    for schedule_row in schedule_service.list_schedules(db, start_date, end_date):
        label = metrics_service.trend_bucket_label(schedule_row.production_date, group_by)
        scheduled_by_bucket[label] = (
            scheduled_by_bucket.get(label, 0) + schedule_row.drawers_scheduled
        )

    all_labels = sorted(set(events_by_bucket) | set(inspected_by_bucket) | set(scheduled_by_bucket))

    # Working Days Logic (Part C addendum): only applies to day-level buckets
    # over a bounded range - a "week" label isn't itself a date, and an
    # unbounded range has no fixed window to bulk-fetch working days for. Both
    # cases fall through with is_working_day left None on every point (see
    # TrendPointOut) - unchanged trend behavior, no flagging/omission.
    working_days: set[dt.date] | None = None
    if group_by == "day" and start_date is not None and end_date is not None:
        working_days = working_days_service.working_day_set(db, start_date, end_date)

    points: list[TrendPointOut] = []
    for label in all_labels:
        is_working: bool | None = None
        if working_days is not None:
            day = dt.date.fromisoformat(label)
            is_working = day in working_days
            if metrics_service.omit_non_working_day_silently(day, is_working):
                continue  # weekend with no working-day override: drop silently

        points.append(
            TrendPointOut(
                period=label,
                defect_events=events_by_bucket.get(label, 0),
                drawers_inspected=inspected_by_bucket.get(label, 0),
                unique_drawers_rejected=rejected_by_bucket.get(label, 0),
                internal_rework_cost=round(rework_cost_by_bucket.get(label, 0.0), 2),
                cost_avoided=round(cost_avoided_by_bucket.get(label, 0.0), 2),
                drawers_scheduled=scheduled_by_bucket.get(label),
                schedule_attainment_pct=metrics_service.compute_schedule_attainment_pct(
                    total_inspected=inspected_by_bucket.get(label, 0),
                    total_scheduled=scheduled_by_bucket.get(label),
                ),
                is_working_day=is_working,
            )
        )
    return points


@router.get("/work-orders/{work_order_number}", response_model=WorkOrderHistoryOut)
def get_work_order_history(
    work_order_number: str, db: Session = Depends(get_db)
) -> WorkOrderHistoryOut:
    cases = (
        db.query(DefectCase)
        .options(
            selectinload(DefectCase.items).selectinload(DefectItem.defect_category),
            selectinload(DefectCase.photos),
            selectinload(DefectCase.status_history),
            selectinload(DefectCase.found_station),
            selectinload(DefectCase.possible_source_station),
        )
        .filter(
            DefectCase.work_order_number == work_order_number,
            DefectCase.is_deleted.is_(False),
        )
        .order_by(DefectCase.detected_at.asc())
        .all()
    )
    total_events = sum(i.affected_drawer_quantity for c in cases for i in c.items)

    # PROJECT_SPEC_PHASE9.md Part 2: group by order AND line, so defects per line
    # are visible rather than only per order - line_label=None groups every case
    # on this work order with no line recorded (pre-Phase-9 rows, or a line that
    # was never filled in). Sorted with the no-line group first, then
    # alphabetically, so a work order with lines A/B/C/no-line always renders in
    # the same predictable order.
    by_line_totals: dict[str | None, dict[str, int]] = {}
    for case in cases:
        bucket = by_line_totals.setdefault(
            case.line_label, {"case_count": 0, "total_defect_events": 0}
        )
        bucket["case_count"] += 1
        bucket["total_defect_events"] += sum(i.affected_drawer_quantity for i in case.items)

    def _sort_key(item: tuple[str | None, dict[str, int]]) -> tuple[bool, str | None]:
        line_label = item[0]
        return (line_label is not None, line_label)

    by_line = [
        WorkOrderLineBreakdownOut(line_label=line_label, **totals)
        for line_label, totals in sorted(by_line_totals.items(), key=_sort_key)
    ]

    return WorkOrderHistoryOut(
        work_order_number=work_order_number,
        cases=[defect_case_to_out(c) for c in cases],
        total_defect_events=total_events,
        by_line=by_line,
    )


@rework_router.get("/rework-queue", response_model=list[ReworkQueueItemOut])
def get_rework_queue(
    db: Session = Depends(get_db),
    priority: str | None = None,
    status: str | None = None,
) -> list[ReworkQueueItemOut]:
    """Open work only, sorted Urgent > High > Normal, oldest first within priority.

    open_statuses reuses defect_service.DIRECT_CLOSE_SOURCE_STATUSES rather than a
    separately hardcoded set - "actionable/open" and "closeable" are the same set
    of statuses now (PROJECT_SPEC_PHASE7.md), so one shared constant keeps them
    from drifting apart."""
    open_statuses = DIRECT_CLOSE_SOURCE_STATUSES
    query = (
        db.query(DefectCase)
        .options(
            selectinload(DefectCase.items).selectinload(DefectItem.defect_category),
            selectinload(DefectCase.found_station),
            selectinload(DefectCase.possible_source_station),
        )
        .filter(DefectCase.is_deleted.is_(False))
    )
    if status is not None:
        query = query.filter(DefectCase.status == status)
    else:
        query = query.filter(DefectCase.status.in_(open_statuses))
    if priority is not None:
        query = query.filter(DefectCase.priority == priority)

    cases = query.all()
    now = dt.datetime.now(dt.timezone.utc)
    ordered = sorted(
        cases, key=lambda c: (metrics_service.priority_sort_index(c.priority), c.detected_at)
    )

    result = []
    for c in ordered:
        detected = (
            c.detected_at if c.detected_at.tzinfo else c.detected_at.replace(tzinfo=dt.timezone.utc)
        )
        age_hours = (now - detected).total_seconds() / 3600
        result.append(
            ReworkQueueItemOut(
                id=c.id,
                case_number=c.case_number,
                work_order_number=c.work_order_number,
                categories=[i.defect_category.name for i in c.items],
                status=c.status,
                disposition=c.disposition,
                priority=c.priority,
                found_station_name=c.found_station.name,
                possible_source_station_name=(
                    c.possible_source_station.name if c.possible_source_station else None
                ),
                detected_at=c.detected_at,
                age_hours=round(age_hours, 1),
                root_cause=c.root_cause,
                corrective_action=c.corrective_action,
                repair_action=c.repair_action,
            )
        )
    return result
