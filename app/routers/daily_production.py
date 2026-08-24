"""Daily Production Summary: the denominators every rate in the app is built from."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.dependencies import get_actor_role, get_db
from app.errors import ValidationError
from app.models import DailyProductionSummary
from app.schemas import (
    DailyProductionSummaryIn,
    DailyProductionSummaryOut,
    DailyScheduleIn,
    DailyScheduleListOut,
    DailyScheduleOut,
    DailySummarySuggestionOut,
    ScheduleAttainmentOut,
)
from app.services import audit_service, defect_service, metrics_service, schedule_service

router = APIRouter(prefix="/api/v1/daily-production", tags=["daily-production"])

# NOTE: /schedule (below) is registered before the dynamic "/{production_date}"
# routes further down this file. Route order matters here - Starlette matches in
# registration order, and "/{production_date}" (an untyped single path segment at
# the routing layer) would otherwise swallow a request for literal "/schedule"
# and fail FastAPI's date-parsing validation instead of ever reaching these.


@router.get("/schedule", response_model=DailyScheduleListOut)
def get_schedule(
    date: dt.date | None = None,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    db: Session = Depends(get_db),
) -> DailyScheduleListOut:
    """Read scheduled drawer count(s) - pass `date` for one day, or `start_date` +
    `end_date` for an inclusive range (dashboard/reports use). A date with no
    daily_schedules row is simply absent from the result - see
    app/services/schedule_service.py."""
    if date is not None:
        row = schedule_service.get_schedule(db, date)
        return DailyScheduleListOut(schedules=[DailyScheduleOut.model_validate(row)] if row else [])
    if start_date is None or end_date is None:
        raise ValidationError(
            "Provide either 'date' or both 'start_date' and 'end_date'.", field="date"
        )
    rows = schedule_service.list_schedules_in_range(db, start_date, end_date)
    return DailyScheduleListOut(schedules=[DailyScheduleOut.model_validate(r) for r in rows])


@router.put("/schedule", response_model=DailyScheduleOut)
def put_schedule(
    payload: DailyScheduleIn,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> DailyScheduleOut:
    """Manual entry/override from the Daily Production Summary form. Always
    source="manual" - pins this date against future sync overwrites (see
    schedule_service.upsert_schedule's manual-wins rule)."""
    row, _applied = schedule_service.upsert_schedule(
        db,
        production_date=payload.production_date,
        drawers_scheduled=payload.drawers_scheduled,
        source=schedule_service.SOURCE_MANUAL,
    )
    audit_service.record(
        db,
        actor_role=actor_role,
        action="upsert",
        entity_type="DailySchedule",
        entity_id=payload.production_date.isoformat(),
        inputs=payload.model_dump(mode="json"),
    )
    return DailyScheduleOut.model_validate(row)


@router.get("/schedule-attainment", response_model=ScheduleAttainmentOut)
def get_schedule_attainment(
    start_date: dt.date, end_date: dt.date, db: Session = Depends(get_db)
) -> ScheduleAttainmentOut:
    """Dashboard's Scheduled vs Completed card (PROJECT_SPEC.md Phase 6 addendum
    5b): one {date, scheduled, inspected} row per calendar day in the range, plus
    range totals and the Schedule Attainment % tile. drawers_inspected is summed
    across every shift that date, so a two-shift day is never double-counted
    against the (whole-day) scheduled figure - see
    app/services/metrics_service.py build_schedule_vs_completed."""
    scheduled_by_date = schedule_service.get_schedules_in_range(db, start_date, end_date)

    summary_rows = (
        db.query(DailyProductionSummary)
        .filter(DailyProductionSummary.production_date >= start_date)
        .filter(DailyProductionSummary.production_date <= end_date)
        .all()
    )
    inspected_by_date: dict[dt.date, int] = {}
    for row in summary_rows:
        inspected_by_date[row.production_date] = (
            inspected_by_date.get(row.production_date, 0) + row.drawers_inspected
        )

    result = metrics_service.build_schedule_vs_completed(
        start_date=start_date,
        end_date=end_date,
        scheduled_by_date=scheduled_by_date,
        inspected_by_date=inspected_by_date,
    )
    return ScheduleAttainmentOut(**result)


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
    out.reworked_case_count = defect_service.count_rework_cases_by_date(db, [production_date]).get(
        production_date, 0
    )
    return out


@router.get("/{production_date}/suggested-counts", response_model=DailySummarySuggestionOut)
def get_suggested_counts(
    production_date: dt.date, db: Session = Depends(get_db)
) -> DailySummarySuggestionOut:
    """Powers the Daily Summary form's auto-calculated suggestion and its
    "Recalculate from defect cases" button (docs/PROJECT_SPEC_PHASE4.md
    "Scrap removal" / auto-calculation). Read-only - never writes to
    DailyProductionSummary, so calling it can never overwrite an already-saved
    entry; the frontend decides when to apply the suggestion to the form fields."""
    return DailySummarySuggestionOut(**defect_service.suggested_daily_counts(db, production_date))


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
    reworked_by_date = defect_service.count_rework_cases_by_date(
        db, [r.production_date for r in rows]
    )
    results = []
    for r in rows:
        out = DailyProductionSummaryOut.model_validate(r)
        out.reworked_case_count = reworked_by_date.get(r.production_date, 0)
        results.append(out)
    return results
