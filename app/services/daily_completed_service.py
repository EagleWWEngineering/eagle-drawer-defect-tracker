"""Daily "drawers completed" feed from eagle-drawers-production-count
(PROJECT_SPEC_PHASE10.md Part 1).

DailyProductionSummary.drawers_inspected is written ONLY here. Production count
(on eagle-vm, next to the production brief) counts unique, valid QC/Sorting
scans per shop-local calendar day - one per drawer that finished the whole line,
repeats/order-only labels/undone scans excluded - and POSTs them to
/api/v1/sync/daily-completed/ingest-raw at 06:00 (finalizes yesterday) and 15:30
(today at shift end) America/New_York, each time re-sending the trailing 7 days
so corrections flow through. The Daily Summary form shows the value read-only
and every manual write path rejects it (app/schemas.py DailyProductionSummaryIn).

Rules, per date in the payload (always shift "Day" - single-shift plant):
  - a (date, "Day") row exists  -> set drawers_inspected, touch nothing else;
  - no row and count > 0        -> create it, other counts 0, cost snapshot
                                   taken the same way a form-created row is;
  - no row and count == 0       -> do nothing (no empty weekend/holiday rows -
                                   the brief treats "row exists" as "entered").
Idempotent by construction: every write is "set to this value".

Unlike the schedule ingest, a non-working day is NOT rejected: a count > 0 on
a Saturday is real QC scans (overtime), and working_days_service already treats
a date with inspected > 0 as a working day.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from app.errors import UnprocessableError
from app.models import DailyProductionSummary, SyncLog
from app.services import settings_service

FEED_SHIFT = "Day"
EXPECTED_SOURCE = "eagle-drawers-production-count"


def validate_payload(data: Any) -> dict[dt.date, int]:
    """Parse and validate the whole body up front, before any write. Any bad
    date or count rejects the entire request (UnprocessableError -> 422) so a
    half-applied batch is impossible.

    Returns {production_date: count}."""
    if not isinstance(data, dict):
        raise UnprocessableError("Body must be a JSON object.")
    counts = data.get("counts")
    if not isinstance(counts, dict):
        raise UnprocessableError("'counts' must be an object of {date: count}.", field="counts")

    parsed: dict[dt.date, int] = {}
    for key, value in counts.items():
        try:
            production_date = dt.date.fromisoformat(key)
        except (TypeError, ValueError):
            raise UnprocessableError(f"'{key}' is not a YYYY-MM-DD date.", field="counts") from None
        # bool is an int subclass in Python - true/false is never a count.
        if isinstance(value, bool) or not isinstance(value, int):
            raise UnprocessableError(f"{key}: count must be an integer.", field="counts")
        if value < 0:
            raise UnprocessableError(f"{key}: count cannot be negative.", field="counts")
        parsed[production_date] = value
    return parsed


def apply_counts(db: Session, counts: dict[dt.date, int]) -> dict[str, int]:
    """Apply already-validated counts without committing. Returns the summary
    counters. Also used directly by tests/seed scripts that need a
    drawers_inspected figure in place (the manual path can't set it)."""
    updated = created = skipped_zero = 0
    rate = None
    for production_date in sorted(counts):
        count = counts[production_date]
        row = (
            db.query(DailyProductionSummary)
            .filter(
                DailyProductionSummary.production_date == production_date,
                DailyProductionSummary.shift == FEED_SHIFT,
            )
            .first()
        )
        if row is not None:
            row.drawers_inspected = count
            updated += 1
            continue
        if count == 0:
            skipped_zero += 1
            continue
        if rate is None:
            rate = settings_service.get_cost_per_drawer(db)
        db.add(
            DailyProductionSummary(
                production_date=production_date,
                shift=FEED_SHIFT,
                drawers_inspected=count,
                drawers_rejected_unique=0,
                drawers_reworked=0,
                drawers_scrapped=0,
                notes=None,
                cost_per_drawer_at_time=rate,
            )
        )
        created += 1
    db.flush()
    return {
        "received": len(counts),
        "updated": updated,
        "created": created,
        "skipped_zero": skipped_zero,
    }


def process_payload(db: Session, data: dict[str, Any]) -> dict[str, int]:
    """Validate, apply and log one feed call as one transaction + one SyncLog
    row (same Admin Sync Log treatment as the other ingest endpoints).
    Raises UnprocessableError before any write if the body is invalid."""
    counts = validate_payload(data)
    started = dt.datetime.now(dt.timezone.utc)
    summary = apply_counts(db, counts)

    source = data.get("source") or EXPECTED_SOURCE
    station = data.get("station") or "QC_SORTING"
    db.add(
        SyncLog(
            sync_started_at=started,
            sync_completed_at=dt.datetime.now(dt.timezone.utc),
            source_url=f"relay:{source}/daily-completed/{station}"[:255],
            records_fetched=summary["received"],
            records_created=summary["created"],
            records_updated=summary["updated"],
            records_skipped=summary["skipped_zero"],
            errors=None,
            status="success",
        )
    )
    db.commit()
    return summary
