"""CSV export endpoint. Respects the same filters as the reports/case-list routes."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session, selectinload

from app.dependencies import get_actor_role, get_db
from app.models import DailyProductionSummary, DefectCase, DefectItem
from app.services import audit_service, export_service, metrics_service, settings_service

router = APIRouter(prefix="/api/v1/exports", tags=["exports"])


@router.get("/defects.csv")
def export_defects_csv(
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    work_order_number: str | None = None,
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

    involved_dates = {case.production_date for _item, case in rows}
    daily_cost_by_date: dict = {}
    if involved_dates:
        summary_rows = (
            db.query(DailyProductionSummary)
            .filter(DailyProductionSummary.production_date.in_(involved_dates))
            .all()
        )
        # A date can have more than one shift saved; sum their costs together
        # rather than picking just one row.
        rows_by_date: dict = defaultdict(list)
        for summary_row in summary_rows:
            rows_by_date[summary_row.production_date].append(summary_row)

        fallback_rate = settings_service.get_cost_per_drawer(db)
        for production_date, date_rows in rows_by_date.items():
            rework_cost, scrap_cost = metrics_service.sum_internal_quality_costs(
                [
                    (r.drawers_reworked, r.drawers_scrapped, r.cost_per_drawer_at_time)
                    for r in date_rows
                ],
                fallback_rate=fallback_rate,
            )
            representative_rate = next(
                (
                    r.cost_per_drawer_at_time
                    for r in date_rows
                    if r.cost_per_drawer_at_time is not None
                ),
                fallback_rate,
            )
            daily_cost_by_date[production_date] = {
                "rate": representative_rate,
                "rework_cost": rework_cost,
                "scrap_cost": scrap_cost,
            }

    csv_text = export_service.build_defect_items_csv(rows, daily_cost_by_date=daily_cost_by_date)

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
