"""Daily drawer schedule (Phase 6): how many drawers the production brief
scheduled to finish each calendar date, the denominator for Schedule Attainment %
against DailyProductionSummary.drawers_inspected (the "actually completed" proxy).

See docs/PRODUCTION_BRIEF_SCHEDULE_SOURCE.md for where the number comes from and
the PROJECT_SPEC.md Phase 6 addendum for the full feature. Kept in its own table
(DailySchedule) rather than a column on DailyProductionSummary - see
app/models.py DailySchedule's docstring - because the scheduled number is a
whole-day figure and DailyProductionSummary is unique on (production_date, shift).

Two ways a row gets written, exactly mirroring app/services/sync_service.py's
two-paths pattern for CustomerIssue:
  1. POST /api/v1/sync/daily-schedule/ingest-raw (app/routers/sync.py) - the local
     relay (scripts/relay_customer_issues.py) forwards a scraped
     {date: drawers_scheduled} map here; this module upserts each with
     source="sync".
  2. PUT /api/v1/daily-production/schedule (app/routers/daily_production.py) - a
     logged-in user edits/overrides the value on the Daily Production Summary
     form; this module upserts with source="manual".
Both call the exact same upsert_schedule() so the manual-wins rule lives in
exactly one place.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from app.errors import ValidationError
from app.models import DailySchedule, SyncLog

SOURCE_SYNC = "sync"
SOURCE_MANUAL = "manual"
VALID_SOURCES = (SOURCE_SYNC, SOURCE_MANUAL)


class ScheduleIngestError(RuntimeError):
    """Raised when a relay ingest-raw request body has the wrong shape. Mirrors
    app/services/sync_service.py's ProductionBriefError - caught by the router
    (app/routers/sync.py) and turned into a clean 400, since a malformed request
    body is the caller's error, not a sync failure to log."""


def get_schedule(db: Session, production_date: dt.date) -> DailySchedule | None:
    """None means "no scheduled figure known for this date" - never confuse with
    a row that has drawers_scheduled == 0 (a real fact: the brief scheduled zero
    drawers that day)."""
    return db.get(DailySchedule, production_date)


def list_schedules(
    db: Session, start_date: dt.date | None = None, end_date: dt.date | None = None
) -> list[DailySchedule]:
    """Every daily_schedules row in [start_date, end_date], ordered by date.
    Either bound may be omitted (mirrors app/routers/reports.py's
    _daily_summary_rows) - used by callers that already treat start/end as
    optional filters, e.g. GET /reports/trend."""
    query = db.query(DailySchedule)
    if start_date is not None:
        query = query.filter(DailySchedule.production_date >= start_date)
    if end_date is not None:
        query = query.filter(DailySchedule.production_date <= end_date)
    return query.order_by(DailySchedule.production_date).all()


def list_schedules_in_range(
    db: Session, start_date: dt.date, end_date: dt.date
) -> list[DailySchedule]:
    """Every daily_schedules row in [start_date, end_date] inclusive, ordered by
    date - what GET /api/v1/daily-production/schedule returns for a range. A date
    with no row simply has no entry in the result; the caller/UI infers a gap by
    comparing against the requested date range, not by seeing a 0."""
    return list_schedules(db, start_date, end_date)


def get_schedules_in_range(
    db: Session, start_date: dt.date, end_date: dt.date
) -> dict[dt.date, int]:
    """{production_date: drawers_scheduled} for every daily_schedules row in
    [start_date, end_date] inclusive - the shape the dashboard/reports services
    want for gap-filling and attainment math. A date with no row is simply absent
    from the dict - callers use `.get(date)` returning None to mean "unknown",
    never 0."""
    return {
        row.production_date: row.drawers_scheduled
        for row in list_schedules_in_range(db, start_date, end_date)
    }


def _apply_schedule(
    db: Session,
    *,
    production_date: dt.date,
    drawers_scheduled: int,
    source: str,
    synced_at: dt.datetime | None = None,
) -> tuple[DailySchedule, bool, bool]:
    """Core manual-wins upsert, without committing - shared by upsert_schedule
    (one write, own transaction) and process_schedule_payload (many writes, one
    shared transaction per sync_logs row, matching sync_service.py's
    process_issues_payload pattern).

    Returns (row, applied, existed_before). `applied` is False exactly when the
    manual-wins rule skipped a sync write against an existing manual row.
    """
    if source not in VALID_SOURCES:
        raise ValidationError(f"source must be one of {VALID_SOURCES}.", field="source")
    if drawers_scheduled < 0:
        raise ValidationError(
            "drawers_scheduled must be zero or greater.", field="drawers_scheduled"
        )

    row = db.get(DailySchedule, production_date)
    existed_before = row is not None
    if row is not None and row.source == SOURCE_MANUAL and source == SOURCE_SYNC:
        return row, False, existed_before

    if row is None:
        row = DailySchedule(production_date=production_date)
        db.add(row)

    row.drawers_scheduled = drawers_scheduled
    row.source = source
    if source == SOURCE_SYNC:
        row.synced_at = synced_at or dt.datetime.now(dt.timezone.utc)
    return row, True, existed_before


def upsert_schedule(
    db: Session,
    *,
    production_date: dt.date,
    drawers_scheduled: int,
    source: str,
    synced_at: dt.datetime | None = None,
) -> tuple[DailySchedule, bool]:
    """Create or update the one row for `production_date`, as its own committed
    transaction. Used by the manual PUT route (app/routers/daily_production.py) -
    a single edit, one commit.

    Manual-wins rule: if a row already exists with source == "manual" and this
    call's source == "sync", the write is skipped entirely - the human's value
    and its "manual" source are left completely untouched, and this returns
    (existing_row, False) so callers can log the skip without treating it as a
    failure. A source == "manual" call always applies, overwriting whatever was
    there (sync or manual) - a human explicitly editing the field always wins
    immediately, no matter what came before.

    Raises ValidationError for a negative count or an unrecognized source - the
    DB CheckConstraints (ck_schedule_nonneg / ck_schedule_source) back this up as
    a last resort, but a service-level check gives routers a clean 400 instead of
    a raw IntegrityError.
    """
    row, applied, _ = _apply_schedule(
        db,
        production_date=production_date,
        drawers_scheduled=drawers_scheduled,
        source=source,
        synced_at=synced_at,
    )
    db.commit()
    db.refresh(row)
    return row, applied


def validate_raw_schedule_payload(data: Any) -> dict[str, Any]:
    """Validate that `data` has the shape process_schedule_payload expects: a dict
    with a "schedules" list. Each entry is {"date": "YYYY-MM-DD", "drawers_scheduled":
    <int, or null/absent meaning "no figure found for that date">}. Raises
    ScheduleIngestError on any shape mismatch - the ingest route (app/routers/
    sync.py) catches this and turns it into a clean 400."""
    if not isinstance(data, dict) or "schedules" not in data:
        raise ScheduleIngestError("Payload is missing the expected 'schedules' field.")
    if not isinstance(data["schedules"], list):
        raise ScheduleIngestError("Payload's 'schedules' field must be a list.")
    return data


def process_schedule_payload(
    db: Session,
    data: dict[str, Any],
    *,
    source_url: str,
    sync_started_at: dt.datetime | None = None,
) -> SyncLog:
    """Turn an already-scraped raw payload (see validate_raw_schedule_payload) into
    daily_schedules upserts, one shared transaction, recorded as one SyncLog row -
    mirrors app/services/sync_service.py process_issues_payload() exactly, so the
    Admin Sync Log page shows both kinds of relay activity the same way.

    Never raises for a bad individual entry - it's skipped, counted, and logged in
    SyncLog.errors so one malformed date can't abort the whole batch. A date with
    no 'drawers_scheduled' (the brief had no "Today's plan" fact for it - see
    docs/PRODUCTION_BRIEF_SCHEDULE_SOURCE.md) is also counted as skipped, not an
    error - there's simply nothing to write for that date.
    """
    now = dt.datetime.now(dt.timezone.utc)
    log = SyncLog(sync_started_at=sync_started_at or now, source_url=source_url, status="failed")

    entries = data.get("schedules") or []
    created = updated = skipped = 0
    error_messages: list[str] = []

    for raw in entries:
        date_label = raw.get("date", "?") if isinstance(raw, dict) else "?"
        try:
            if not isinstance(raw, dict) or not raw.get("date"):
                raise ValueError("entry is missing required 'date'")
            production_date = dt.date.fromisoformat(raw["date"])
            count = raw.get("drawers_scheduled")
            if count is None:
                skipped += 1
                continue
            count = int(count)
        except (ValueError, TypeError) as exc:
            skipped += 1
            error_messages.append(f"{date_label}: {exc}")
            continue

        try:
            _row, applied, existed_before = _apply_schedule(
                db,
                production_date=production_date,
                drawers_scheduled=count,
                source=SOURCE_SYNC,
                synced_at=now,
            )
        except ValidationError as exc:
            skipped += 1
            error_messages.append(f"{production_date}: {exc.message}")
            continue

        if not applied:
            # Manual-wins skip - the human's value stands, not an error.
            skipped += 1
            continue
        if existed_before:
            updated += 1
        else:
            created += 1

    log.records_fetched = len(entries)
    log.records_created = created
    log.records_updated = updated
    log.records_skipped = skipped
    log.errors = "; ".join(error_messages) or None
    log.status = "success"
    log.sync_completed_at = dt.datetime.now(dt.timezone.utc)
    db.add(log)
    db.commit()
    db.refresh(log)
    return log
