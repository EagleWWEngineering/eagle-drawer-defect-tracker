"""CSV export endpoint. Respects the same filters as the reports/case-list routes."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session, selectinload

from app.dependencies import get_actor_role, get_db
from app.models import DefectCase, DefectItem
from app.services import audit_service, export_service, metrics_service

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
    csv_text = export_service.build_defect_items_csv(rows)

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
