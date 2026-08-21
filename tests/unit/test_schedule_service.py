"""Unit tests for app/services/schedule_service.py (Phase 6).

See docs/PRODUCTION_BRIEF_SCHEDULE_SOURCE.md and the PROJECT_SPEC.md Phase 6
addendum for the feature this backs.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.errors import ValidationError
from app.models import DailySchedule, SyncLog
from app.services import schedule_service

# ---------------------------------------------------------------------------
# upsert_schedule: create, sync-overwrites-sync, sync-skips-manual,
# manual-overwrites-sync (+ manual-overwrites-manual)
# ---------------------------------------------------------------------------


def test_upsert_creates_a_new_row(db_session):
    row, applied = schedule_service.upsert_schedule(
        db_session,
        production_date=dt.date(2026, 8, 20),
        drawers_scheduled=400,
        source="sync",
    )
    assert applied is True
    assert row.drawers_scheduled == 400
    assert row.source == "sync"
    assert row.synced_at is not None


def test_sync_overwrites_a_prior_sync_row(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=dt.date(2026, 8, 20), drawers_scheduled=400, source="sync"
    )
    row, applied = schedule_service.upsert_schedule(
        db_session, production_date=dt.date(2026, 8, 20), drawers_scheduled=431, source="sync"
    )
    assert applied is True
    assert row.drawers_scheduled == 431
    assert row.source == "sync"


def test_sync_skips_an_existing_manual_row(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=dt.date(2026, 8, 20), drawers_scheduled=999, source="manual"
    )
    row, applied = schedule_service.upsert_schedule(
        db_session, production_date=dt.date(2026, 8, 20), drawers_scheduled=431, source="sync"
    )
    assert applied is False
    # The human's value and source are completely untouched.
    assert row.drawers_scheduled == 999
    assert row.source == "manual"


def test_manual_overwrites_an_existing_sync_row(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=dt.date(2026, 8, 20), drawers_scheduled=431, source="sync"
    )
    row, applied = schedule_service.upsert_schedule(
        db_session, production_date=dt.date(2026, 8, 20), drawers_scheduled=500, source="manual"
    )
    assert applied is True
    assert row.drawers_scheduled == 500
    assert row.source == "manual"


def test_manual_overwrites_an_existing_manual_row(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=dt.date(2026, 8, 20), drawers_scheduled=500, source="manual"
    )
    row, applied = schedule_service.upsert_schedule(
        db_session, production_date=dt.date(2026, 8, 20), drawers_scheduled=525, source="manual"
    )
    assert applied is True
    assert row.drawers_scheduled == 525


def test_upsert_rejects_a_negative_count(db_session):
    with pytest.raises(ValidationError) as exc_info:
        schedule_service.upsert_schedule(
            db_session, production_date=dt.date(2026, 8, 20), drawers_scheduled=-1, source="manual"
        )
    assert exc_info.value.field == "drawers_scheduled"


def test_upsert_rejects_an_unknown_source(db_session):
    with pytest.raises(ValidationError) as exc_info:
        schedule_service.upsert_schedule(
            db_session, production_date=dt.date(2026, 8, 20), drawers_scheduled=1, source="bogus"
        )
    assert exc_info.value.field == "source"


# ---------------------------------------------------------------------------
# Range queries with gaps
# ---------------------------------------------------------------------------


def test_get_schedules_in_range_omits_dates_with_no_row(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=dt.date(2026, 8, 20), drawers_scheduled=431, source="sync"
    )
    result = schedule_service.get_schedules_in_range(
        db_session, dt.date(2026, 8, 19), dt.date(2026, 8, 21)
    )
    assert result == {dt.date(2026, 8, 20): 431}
    assert dt.date(2026, 8, 19) not in result
    assert dt.date(2026, 8, 21) not in result


def test_get_schedule_returns_none_for_a_date_with_no_row(db_session):
    assert schedule_service.get_schedule(db_session, dt.date(2026, 8, 20)) is None


def test_list_schedules_in_range_is_ordered_and_excludes_out_of_range_rows(db_session):
    for d, n in [(dt.date(2026, 8, 18), 1), (dt.date(2026, 8, 20), 2), (dt.date(2026, 8, 25), 3)]:
        schedule_service.upsert_schedule(
            db_session, production_date=d, drawers_scheduled=n, source="sync"
        )
    rows = schedule_service.list_schedules_in_range(
        db_session, dt.date(2026, 8, 18), dt.date(2026, 8, 20)
    )
    assert [r.production_date for r in rows] == [dt.date(2026, 8, 18), dt.date(2026, 8, 20)]


def test_list_schedules_optional_bounds(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=dt.date(2026, 8, 20), drawers_scheduled=1, source="sync"
    )
    assert len(schedule_service.list_schedules(db_session)) == 1
    assert len(schedule_service.list_schedules(db_session, start_date=dt.date(2026, 8, 21))) == 0


# ---------------------------------------------------------------------------
# Relay ingest processing (process_schedule_payload / validate_raw_schedule_payload)
# ---------------------------------------------------------------------------


def test_validate_raw_schedule_payload_rejects_bad_shapes(db_session):
    for bad in [{}, {"schedules": "not-a-list"}, {"schedules": None}]:
        with pytest.raises(schedule_service.ScheduleIngestError):
            schedule_service.validate_raw_schedule_payload(bad)


def test_process_schedule_payload_creates_and_updates(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=dt.date(2026, 8, 19), drawers_scheduled=100, source="sync"
    )
    payload = {
        "schedules": [
            {"date": "2026-08-19", "drawers_scheduled": 150},  # updates
            {"date": "2026-08-20", "drawers_scheduled": 431},  # creates
        ]
    }
    log = schedule_service.process_schedule_payload(db_session, payload, source_url="relay:test")

    assert log.status == "success"
    assert log.records_fetched == 2
    assert log.records_created == 1
    assert log.records_updated == 1
    assert log.records_skipped == 0
    assert schedule_service.get_schedule(db_session, dt.date(2026, 8, 19)).drawers_scheduled == 150
    assert schedule_service.get_schedule(db_session, dt.date(2026, 8, 20)).drawers_scheduled == 431
    assert db_session.query(SyncLog).count() == 1


def test_process_schedule_payload_null_drawers_scheduled_is_skipped_not_written(db_session):
    payload = {"schedules": [{"date": "2026-08-19", "drawers_scheduled": None}]}
    log = schedule_service.process_schedule_payload(db_session, payload, source_url="relay:test")

    assert log.records_skipped == 1
    assert log.records_created == 0
    assert schedule_service.get_schedule(db_session, dt.date(2026, 8, 19)) is None


def test_process_schedule_payload_manual_wins_counts_as_skipped(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=dt.date(2026, 8, 19), drawers_scheduled=999, source="manual"
    )
    payload = {"schedules": [{"date": "2026-08-19", "drawers_scheduled": 150}]}
    log = schedule_service.process_schedule_payload(db_session, payload, source_url="relay:test")

    assert log.records_skipped == 1
    assert log.records_created == 0
    assert log.records_updated == 0
    row = schedule_service.get_schedule(db_session, dt.date(2026, 8, 19))
    assert row.drawers_scheduled == 999
    assert row.source == "manual"


def test_process_schedule_payload_one_bad_entry_does_not_abort_the_batch(db_session):
    payload = {
        "schedules": [
            {"date": "not-a-date", "drawers_scheduled": 100},
            {"date": "2026-08-20", "drawers_scheduled": 431},
        ]
    }
    log = schedule_service.process_schedule_payload(db_session, payload, source_url="relay:test")

    assert log.status == "success"
    assert log.records_skipped == 1
    assert log.records_created == 1
    assert log.errors is not None
    assert schedule_service.get_schedule(db_session, dt.date(2026, 8, 20)).drawers_scheduled == 431


def test_process_schedule_payload_missing_date_key_is_skipped(db_session):
    payload = {"schedules": [{"drawers_scheduled": 100}]}
    log = schedule_service.process_schedule_payload(db_session, payload, source_url="relay:test")

    assert log.records_skipped == 1
    assert db_session.query(DailySchedule).count() == 0
