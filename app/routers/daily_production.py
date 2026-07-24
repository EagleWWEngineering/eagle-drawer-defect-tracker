"""Daily Production Summary: the denominators every rate in the app is built from."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.dependencies import get_actor_role, get_db
from app.models import DailyProductionSummary
from app.schemas import DailyProductionSummaryIn, DailyProductionSummaryOut
from app.services import audit_service, defect_service

router = APIRouter(prefix="/api/v1/daily-production", tags=["daily-production"])


@router.put("/{production_date}", response_model=DailyProductionSummaryOut)
def upsert_summary(
    production_date: dt.date,
    payload: DailyProductionSummaryIn,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> DailyProductionSummaryOut:
    row, warnings = defect_service.upsert_daily_summary(
        db,
        production_date=production_date,
        shift=payload.shift,
        drawers_inspected=payload.drawers_inspected,
        drawers_rejected_unique=payload.drawers_rejected_unique,
        drawers_reworked=payload.drawers_reworked,
        drawers_scrapped=payload.drawers_scrapped,
        notes=payload.notes,
    )
    audit_service.record(
        db,
        actor_role=actor_role,
        action="upsert",
        entity_type="DailyProductionSummary",
        entity_id=f"{production_date}:{payload.shift}",
        inputs=payload.model_dump(mode="json"),
        message="; ".join(warnings) if warnings else None,
    )
    out = DailyProductionSummaryOut.model_validate(row)
    out.warnings = warnings
    return out


@router.get("", response_model=list[DailyProductionSummaryOut])
def list_summaries(
    db: Session = Depends(get_db),
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
) -> list[DailyProductionSummaryOut]:
    query = db.query(DailyProductionSummary)
    if start_date is not None:
        query = query.filter(DailyProductionSummary.production_date >= start_date)
    if end_date is not None:
        query = query.filter(DailyProductionSummary.production_date <= end_date)
    rows = query.order_by(DailyProductionSummary.production_date.desc()).all()
    return [DailyProductionSummaryOut.model_validate(r) for r in rows]
