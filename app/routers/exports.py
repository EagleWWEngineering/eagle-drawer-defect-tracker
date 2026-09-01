"""CSV export endpoint. Respects the same filters as the reports/case-list routes."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session, selectinload

from app.dependencies import get_actor_role, get_db
from app.models import DailyProductionSummary, DefectCase, DefectItem
from app.services import (
    audit_service,
    export_service,
    metrics_service,
    schedule_service,
    settings_service,
)

router = APIRouter(prefix="/api/v1/exports", tags=["exports"])


@router.get("/defects.csv")
def export_defects_csv(
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
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
) -> Response:
    query = metrics_service.filtered_defect_items_query(
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
    ).options(
        selectinload(DefectCase.found_station),
        selectinload(DefectCase.possible_source_station),
        selectinload(DefectItem.defect_category),
    )
    rows = query.all()

    # Phase 7 "Cost model": cost is per-CASE now (see export_service.py
    # build_defect_items_csv), computed straight from each row's own case - no
    # date-joined DailyProductionSummary lookup needed for cost anymore.
    fallback_rate = settings_service.get_cost_per_drawer(db)

    # Phase 6: same-day schedule + attainment context, joined by production_date.
    # Still needs DailyProductionSummary rows, to sum drawers_inspected across
    # shifts before computing attainment, so a two-shift day is never
    # double-counted against the whole-day scheduled figure.
    involved_dates = {case.production_date for _item, case in rows}
    daily_schedule_by_date: dict = {}
    if involved_dates:
        summary_rows = (
            db.query(DailyProductionSummary)
            .filter(DailyProductionSummary.production_date.in_(involved_dates))
            .all()
        )
        inspected_by_date: dict = defaultdict(int)
        for summary_row in summary_rows:
            inspected_by_date[summary_row.production_date] += summary_row.drawers_inspected

        schedule_rows = schedule_service.list_schedules(
            db, min(involved_dates), max(involved_dates)
        )
        for schedule_row in schedule_rows:
            if schedule_row.production_date not in involved_dates:
                continue
            daily_schedule_by_date[schedule_row.production_date] = {
                "drawers_scheduled": schedule_row.drawers_scheduled,
                "attainment_pct": metrics_service.compute_schedule_attainment_pct(
                    total_inspected=inspected_by_date.get(schedule_row.production_date, 0),
                    total_scheduled=schedule_row.drawers_scheduled,
                ),
            }

    csv_text = export_service.build_defect_items_csv(
        rows, fallback_rate=fallback_rate, daily_schedule_by_date=daily_schedule_by_date
    )

    audit_service.record(
        db,
        actor_role=actor_role,
        action="export",
        entity_type="DefectCase",
        entity_id=None,
        inputs={
            "start_date": str(start_date) if start_date else None,
            "end_date": str(end_date) if end_date else None,
            "work_order_number": work_order_number,
            "line_label": line_label,
            "category_id": category_id,
            "found_station_id": found_station_id,
            "possible_source_station_id": possible_source_station_id,
            "priority": priority,
            "status": status,
            "disposition": disposition,
        },
        message=f"{len(rows)} rows exported",
    )

    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=defects.csv"},
    )
