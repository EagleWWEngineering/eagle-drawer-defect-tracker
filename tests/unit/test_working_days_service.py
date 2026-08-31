"""Unit tests for app/services/working_days_service.py (Part C: Working Days
Logic) - the single place the working-day definition lives.

2026-08-17 is a Monday, 2026-08-22 is a Saturday, 2026-08-23 is a Sunday - dates
chosen to land on real weekdays/weekends without relying on any timezone/display
logic (this service is pure DB + calendar math, no timezone_utils dependency).
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.errors import ServiceError, ValidationError
from app.models import DailyProductionSummary
from app.services import schedule_service, working_days_service

MONDAY = dt.date(2026, 8, 17)
FRIDAY = dt.date(2026, 8, 21)
SATURDAY = dt.date(2026, 8, 22)
SUNDAY = dt.date(2026, 8, 23)
NEXT_MONDAY = dt.date(2026, 8, 24)


def _add_summary(db, production_date: dt.date, drawers_inspected: int, shift: str = "Day") -> None:
    db.add(
        DailyProductionSummary(
            production_date=production_date,
            shift=shift,
            drawers_inspected=drawers_inspected,
            drawers_rejected_unique=0,
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# is_working_day
# ---------------------------------------------------------------------------


def test_plain_weekday_with_a_schedule_is_a_working_day(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=FRIDAY, drawers_scheduled=400, source="sync"
    )
    assert working_days_service.is_working_day(db_session, FRIDAY) is True


def test_saturday_with_no_rows_is_not_a_working_day(db_session):
    assert working_days_service.is_working_day(db_session, SATURDAY) is False


def test_saturday_with_a_manual_schedule_row_is_a_working_day(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=SATURDAY, drawers_scheduled=120, source="manual"
    )
    assert working_days_service.is_working_day(db_session, SATURDAY) is True


def test_saturday_with_zero_schedule_but_inspections_is_a_working_day(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=SATURDAY, drawers_scheduled=0, source="manual"
    )
    _add_summary(db_session, SATURDAY, drawers_inspected=15)
    assert working_days_service.is_working_day(db_session, SATURDAY) is True


def test_weekday_with_zero_scheduled_and_zero_inspected_is_not_a_working_day(db_session):
    """A real holiday/shutdown: the brief ran and explicitly scheduled 0, and
    nothing was inspected either."""
    schedule_service.upsert_schedule(
        db_session, production_date=FRIDAY, drawers_scheduled=0, source="sync"
    )
    assert working_days_service.is_working_day(db_session, FRIDAY) is False


def test_weekday_with_zero_scheduled_but_inspected_is_a_working_day(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=FRIDAY, drawers_scheduled=0, source="sync"
    )
    _add_summary(db_session, FRIDAY, drawers_inspected=10)
    assert working_days_service.is_working_day(db_session, FRIDAY) is True


def test_weekday_with_no_schedule_row_at_all_is_still_a_working_day(db_session):
    """The failed-scrape case: no daily_schedules row, no inspections either. Must
    NOT disappear - a missing row is never treated as a scheduled zero."""
    assert working_days_service.is_working_day(db_session, FRIDAY) is True


def test_weekday_with_positive_schedule_is_a_working_day_even_with_zero_inspected(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=FRIDAY, drawers_scheduled=250, source="sync"
    )
    assert working_days_service.is_working_day(db_session, FRIDAY) is True


# ---------------------------------------------------------------------------
# walk_back_working_days / previous_working_day
# ---------------------------------------------------------------------------


def test_walk_back_from_monday_lands_on_previous_friday(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=FRIDAY, drawers_scheduled=400, source="sync"
    )
    result = working_days_service.walk_back_working_days(db_session, NEXT_MONDAY, 1)
    assert result == FRIDAY


def test_walk_back_from_monday_skips_a_holiday_friday_and_lands_on_thursday(db_session):
    thursday = dt.date(2026, 8, 20)
    schedule_service.upsert_schedule(
        db_session, production_date=thursday, drawers_scheduled=380, source="sync"
    )
    # Friday: brief ran, scheduled 0, nothing inspected - a holiday, not a gap.
    schedule_service.upsert_schedule(
        db_session, production_date=FRIDAY, drawers_scheduled=0, source="sync"
    )
    result = working_days_service.walk_back_working_days(db_session, NEXT_MONDAY, 1)
    assert result == thursday


def test_previous_working_day_is_an_alias_for_walk_back_one(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=FRIDAY, drawers_scheduled=400, source="sync"
    )
    assert working_days_service.previous_working_day(
        db_session, NEXT_MONDAY
    ) == working_days_service.walk_back_working_days(db_session, NEXT_MONDAY, 1)


def test_walk_back_with_no_data_falls_back_to_plain_weekday(db_session):
    """Brand-new DB, no rows anywhere - Mon-Fri is the fallback."""
    result = working_days_service.walk_back_working_days(db_session, NEXT_MONDAY, 1)
    assert result == FRIDAY


def test_walk_back_raises_service_error_when_nothing_found_in_the_window(db_session):
    """Every single day in the 60-day lookback window is an explicit
    scheduled-0/zero-inspected weekday or a weekend - no working day anywhere."""
    d = NEXT_MONDAY
    for _ in range(working_days_service._MAX_WALK_BACK_DAYS + 5):
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            schedule_service.upsert_schedule(
                db_session, production_date=d, drawers_scheduled=0, source="sync"
            )
    with pytest.raises(ServiceError):
        working_days_service.walk_back_working_days(db_session, NEXT_MONDAY, 1)


# ---------------------------------------------------------------------------
# working_day_set
# ---------------------------------------------------------------------------


def test_working_day_set_over_two_weeks_returns_exactly_the_ten_weekdays(db_session):
    start = MONDAY
    end = MONDAY + dt.timedelta(days=13)  # two full Mon-Sun weeks
    result = working_days_service.working_day_set(db_session, start, end)
    expected = {
        start + dt.timedelta(days=i)
        for i in range(14)
        if (start + dt.timedelta(days=i)).weekday() < 5
    }
    assert result == expected
    assert len(result) == 10


def test_working_day_set_issues_at_most_two_queries(db_session, monkeypatch):
    import app.services.working_days_service as wds

    original_query = db_session.query
    call_count = {"n": 0}

    def counting_query(*args, **kwargs):
        call_count["n"] += 1
        return original_query(*args, **kwargs)

    monkeypatch.setattr(db_session, "query", counting_query)

    wds.working_day_set(db_session, MONDAY, MONDAY + dt.timedelta(days=13))
    assert call_count["n"] <= 2


def test_working_day_set_reflects_manual_saturday_and_holiday_weekday(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=SATURDAY, drawers_scheduled=50, source="manual"
    )
    schedule_service.upsert_schedule(
        db_session, production_date=FRIDAY, drawers_scheduled=0, source="sync"
    )
    result = working_days_service.working_day_set(db_session, FRIDAY, SUNDAY)
    assert FRIDAY not in result  # holiday
    assert SATURDAY in result  # overtime Saturday
    assert SUNDAY not in result  # plain weekend


def test_working_day_set_empty_range_when_start_after_end(db_session):
    assert working_days_service.working_day_set(db_session, FRIDAY, MONDAY) == set()


# ---------------------------------------------------------------------------
# resolve_working_day_preset: "yesterday" / "last_7_days" / "last_30_days"
# ---------------------------------------------------------------------------


def test_preset_yesterday_from_a_monday_is_the_previous_friday(db_session):
    schedule_service.upsert_schedule(
        db_session, production_date=FRIDAY, drawers_scheduled=400, source="sync"
    )
    start, end = working_days_service.resolve_working_day_preset(
        db_session, "yesterday", today=NEXT_MONDAY
    )
    assert start == end == FRIDAY


def test_preset_last_7_days_ending_on_a_working_day_counts_today(db_session):
    """Today (a Thursday, a working day with no rows - Mon-Fri fallback) counts
    as one of the 7 - so start is 6 working days before it."""
    thursday = dt.date(2026, 8, 20)  # a working day (no rows, weekday fallback)
    start, end = working_days_service.resolve_working_day_preset(
        db_session, "last_7_days", today=thursday
    )
    assert end == thursday
    # 6 working days back from Thursday 8/20, skipping the weekend in between:
    # Wed 8/19, Tue 8/18, Mon 8/17, Fri 8/14, Thu 8/13, Wed 8/12.
    assert start == dt.date(2026, 8, 12)


def test_preset_last_7_days_ending_on_a_non_working_day_does_not_count_today(db_session):
    """Today is the Saturday, with no rows - not a working day. It still ends
    the range (end_date == today), but doesn't count toward the 7."""
    start, end = working_days_service.resolve_working_day_preset(
        db_session, "last_7_days", today=SATURDAY
    )
    assert end == SATURDAY
    # 7 working days strictly before Saturday 8/22: Fri 8/21 back through
    # Thu 8/13.
    assert start == dt.date(2026, 8, 13)


def test_preset_last_30_days_returns_a_wider_window_than_last_7(db_session):
    start_7, _ = working_days_service.resolve_working_day_preset(
        db_session, "last_7_days", today=NEXT_MONDAY
    )
    start_30, end_30 = working_days_service.resolve_working_day_preset(
        db_session, "last_30_days", today=NEXT_MONDAY
    )
    assert end_30 == NEXT_MONDAY
    assert start_30 < start_7


def test_preset_last_7_days_baseline_without_a_holiday(db_session):
    """6 working days strictly before Monday 8/24, no holidays: Fri 8/21, Thu
    8/20, Wed 8/19, Tue 8/18, Mon 8/17, Fri 8/14 - the 6th is 8/14."""
    start, end = working_days_service.resolve_working_day_preset(
        db_session, "last_7_days", today=NEXT_MONDAY
    )
    assert end == NEXT_MONDAY
    assert start == dt.date(2026, 8, 14)


def test_preset_last_7_days_excludes_a_holiday_from_the_count(db_session):
    """Same window as the baseline above, except Friday 8/21 was a holiday
    (scheduled 0, inspected 0) - it must not count as one of the 7 working days,
    so the window reaches one calendar day further back (8/13, not 8/14)."""
    schedule_service.upsert_schedule(
        db_session, production_date=FRIDAY, drawers_scheduled=0, source="sync"
    )
    start, end = working_days_service.resolve_working_day_preset(
        db_session, "last_7_days", today=NEXT_MONDAY
    )
    assert end == NEXT_MONDAY
    assert start == dt.date(2026, 8, 13)


def test_resolve_working_day_preset_rejects_a_calendar_only_preset(db_session):
    with pytest.raises(ValidationError) as exc_info:
        working_days_service.resolve_working_day_preset(db_session, "today", today=MONDAY)
    assert exc_info.value.field == "preset"
