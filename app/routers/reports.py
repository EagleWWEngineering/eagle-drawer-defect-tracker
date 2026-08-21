"""Dashboard KPIs, Pareto, trend, work-order drilldown, and the rework queue.

All counting math is delegated to app/services/metrics_service.py so the numbers
shown here can never drift from the numbers in exports or MCP tool results.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, selectinload

from app.dependencies import get_db
from app.models import DailyProductionSummary, DefectCase, DefectItem, StatusHistory
from app.schemas import (
    DatePresetOut,
    KpiOut,
    ParetoRowOut,
    ReworkQueueItemOut,
    TrendPointOut,
    WorkOrderHistoryOut,
    defect_case_to_out,
)
from app.services import metrics_service, schedule_service, settings_service
from app.timezone_utils import resolve_date_preset

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])
rework_router = APIRouter(prefix="/api/v1", tags=["rework-queue"])


@router.get("/date-preset", response_model=DatePresetOut)
def get_date_preset(preset: str) -> DatePresetOut:
    """Resolve one of the Dashboard's date-range preset buttons (Today/Yesterday/
    Last 7 days/Last 30 days/Month to date) to concrete {start_date, end_date},
    in DISPLAY_TIMEZONE - see app/timezone_utils.py resolve_date_preset(). The
    dashboard calls this rather than computing the boundary itself in JavaScript,
    so there is exactly one implementation of "what does 'Yesterday' mean" to get
    right and test."""
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
        "drawers_reworked": sum(r.drawers_reworked for r in rows),
    }


def _distinct_cases(items: list[tuple[DefectItem, DefectCase]]) -> list[DefectCase]:
    """De-dupe a (DefectItem, DefectCase) result set down to its distinct cases -
    a case with N items appears N times in `items`."""
    cases_by_id: dict[int, DefectCase] = {}
    for _item, case in items:
        cases_by_id[case.id] = case
    return list(cases_by_id.values())


def _reached_in_rework_case_ids(db: Session, case_ids: list[int]) -> set[int]:
    """Case ids (within `case_ids`) whose status_history shows they reached "In
    Rework" at some point - the denominator for the "% Queued Rework Closed Without
    Recheck" KPI (PROJECT_SPEC.md section 3.3). Cases resolved on the spot at entry
    never pass through "In Rework", so they're correctly excluded here."""
    if not case_ids:
        return set()
    rows = (
        db.query(StatusHistory.defect_case_id)
        .filter(StatusHistory.defect_case_id.in_(case_ids), StatusHistory.to_status == "In Rework")
        .distinct()
        .all()
    )
    return {row[0] for row in rows}


def _fallback_case_rework_count(
    items: list[tuple[DefectItem, DefectCase]], summary_rows: list[DailyProductionSummary]
) -> int:
    """Defect-case-derived reworked count (Phase 4 cost fix), limited to cases whose
    production_date has no DailyProductionSummary row at all - see
    metrics_service.compute_internal_quality_cost for why that's the fallback rule."""
    summary_dates = {r.production_date for r in summary_rows}
    fallback_cases = [c for c in _distinct_cases(items) if c.production_date not in summary_dates]
    return metrics_service.defect_case_derived_rework_count(
        [(c.production_date, c.status, c.disposition) for c in fallback_cases]
    )


@router.get("/summary", response_model=KpiOut)
def get_summary(
    db: Session = Depends(get_db),
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    work_order_number: str | None = None,
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
        category_id=category_id,
        found_station_id=found_station_id,
        possible_source_station_id=possible_source_station_id,
        priority=priority,
        status=status,
        disposition=disposition,
    )
    items = items_query.all()
    defect_events = sum(item.affected_drawer_quantity for item, _case in items)
    summary_rows = _daily_summary_rows(db, start_date, end_date)
    totals = _daily_totals(summary_rows)

    fallback_rate = settings_service.get_cost_per_drawer(db)
    cost_result = metrics_service.compute_internal_quality_cost(
        daily_summary_entries=[
            (r.drawers_reworked, r.cost_per_drawer_at_time) for r in summary_rows
        ],
        fallback_case_rework_count=_fallback_case_rework_count(items, summary_rows),
        fallback_rate=fallback_rate,
        has_daily_summary_rows=bool(summary_rows),
    )

    kpis = metrics_service.compute_kpis(
        drawers_inspected=totals["drawers_inspected"],
        defect_events=defect_events,
        unique_drawers_rejected=totals["drawers_rejected_unique"],
        drawers_reworked=totals["drawers_reworked"],
        internal_rework_cost=cost_result["internal_rework_cost"],
    ).to_dict()
    kpis["defect_case_rework_count"] = cost_result["defect_case_rework_count"]
    kpis["cost_basis"] = cost_result["cost_basis"]

    # PROJECT_SPEC.md section 3.3 KPIs (60-second-fix fast paths).
    cases = _distinct_cases(items)
    total_cases = len(cases)
    resolved_on_the_spot_count = sum(1 for c in cases if c.resolved_on_the_spot)
    reached_in_rework_ids = _reached_in_rework_case_ids(db, [c.id for c in cases])
    queued_rework_count = len(reached_in_rework_ids)
    skipped_recheck_count = sum(
        1 for c in cases if c.id in reached_in_rework_ids and c.skipped_recheck
    )
    kpis["total_cases"] = total_cases
    kpis["resolved_on_the_spot_count"] = resolved_on_the_spot_count
    kpis["pct_resolved_on_the_spot"] = metrics_service.compute_resolved_on_the_spot_rate(
        total_cases=total_cases, resolved_on_the_spot_count=resolved_on_the_spot_count
    )
    kpis["queued_rework_count"] = queued_rework_count
    kpis["skipped_recheck_count"] = skipped_recheck_count
    kpis["pct_queued_rework_closed_without_recheck"] = metrics_service.compute_skip_recheck_rate(
        queued_rework_count=queued_rework_count, skipped_recheck_count=skipped_recheck_count
    )
    return KpiOut(**kpis)


@router.get("/pareto", response_model=list[ParetoRowOut])
def get_pareto(
    db: Session = Depends(get_db),
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    work_order_number: str | None = None,
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
    summary_dates = {r.production_date for r in summary_rows}
    fallback_rate = settings_service.get_cost_per_drawer(db)

    inspected_by_bucket: dict[str, int] = {}
    rejected_by_bucket: dict[str, int] = {}
    rework_cost_by_bucket: dict[str, float] = {}
    for row in summary_rows:
        label = metrics_service.trend_bucket_label(row.production_date, group_by)
        inspected_by_bucket[label] = inspected_by_bucket.get(label, 0) + row.drawers_inspected
        rejected_by_bucket[label] = rejected_by_bucket.get(label, 0) + row.drawers_rejected_unique
        rework_cost = metrics_service.sum_internal_rework_cost(
            [(row.drawers_reworked, row.cost_per_drawer_at_time)],
            fallback_rate=fallback_rate,
        )
        rework_cost_by_bucket[label] = rework_cost_by_bucket.get(label, 0.0) + rework_cost

    # Phase 4 cost fix: add defect-case-derived rework cost for buckets whose cases
    # fall on a production_date with no DailyProductionSummary row at all, so this
    # chart can never disagree with /reports/summary over the same date range
    # (PROJECT_SPEC.md section 9: "chart totals must match the filtered record total").
    fallback_cases_by_bucket: dict[str, list[tuple[dt.date, str, str | None]]] = {}
    for case in _distinct_cases(items):
        if case.production_date in summary_dates:
            continue
        label = metrics_service.trend_bucket_label(case.production_date, group_by)
        fallback_cases_by_bucket.setdefault(label, []).append(
            (case.production_date, case.status, case.disposition)
        )
    for label, case_tuples in fallback_cases_by_bucket.items():
        reworked = metrics_service.defect_case_derived_rework_count(case_tuples)
        rework_cost_by_bucket[label] = rework_cost_by_bucket.get(label, 0.0) + reworked * float(
            fallback_rate
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
    return [
        TrendPointOut(
            period=label,
            defect_events=events_by_bucket.get(label, 0),
            drawers_inspected=inspected_by_bucket.get(label, 0),
            unique_drawers_rejected=rejected_by_bucket.get(label, 0),
            internal_rework_cost=round(rework_cost_by_bucket.get(label, 0.0), 2),
            drawers_scheduled=scheduled_by_bucket.get(label),
            schedule_attainment_pct=metrics_service.compute_schedule_attainment_pct(
                total_inspected=inspected_by_bucket.get(label, 0),
                total_scheduled=scheduled_by_bucket.get(label),
            ),
        )
        for label in all_labels
    ]


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
    return WorkOrderHistoryOut(
        work_order_number=work_order_number,
        cases=[defect_case_to_out(c) for c in cases],
        total_defect_events=total_events,
    )


@rework_router.get("/rework-queue", response_model=list[ReworkQueueItemOut])
def get_rework_queue(
    db: Session = Depends(get_db),
    priority: str | None = None,
    status: str | None = None,
) -> list[ReworkQueueItemOut]:
    """Open work only, sorted Urgent > High > Normal, oldest first within priority."""
    open_statuses = {"Open", "In Rework", "Waiting", "Ready for QC Recheck"}
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
