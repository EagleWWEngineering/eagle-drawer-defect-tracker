"""Unit tests for app/timezone_utils.py resolve_date_preset() - the Dashboard's
date-range presets (PROJECT_SPEC.md Phase 6 addendum 5a).

Every boundary is asserted at a moment where UTC has already rolled to the next
calendar day but America/New_York (DISPLAY_TIMEZONE) has not - 2026-08-22 03:30
UTC is 2026-08-21 23:30 EDT. A naive implementation using UTC (or the server/
browser's local time) would compute "today" as 2026-08-22 here and every
assertion below would fail.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.errors import ValidationError
from app.timezone_utils import resolve_date_preset

NEAR_UTC_BOUNDARY = dt.datetime(2026, 8, 22, 3, 30, tzinfo=dt.timezone.utc)
EXPECTED_TODAY = dt.date(2026, 8, 21)


def test_today_resolves_in_display_timezone_not_utc():
    assert resolve_date_preset("today", now=NEAR_UTC_BOUNDARY) == (EXPECTED_TODAY, EXPECTED_TODAY)


@pytest.mark.parametrize("preset", ["yesterday", "last_7_days", "last_30_days"])
def test_working_day_presets_are_no_longer_resolved_here(preset):
    """Working Days Logic (Part C addendum): these three presets moved to
    app/services/working_days_service.resolve_working_day_preset() (see
    tests/unit/test_working_days_service.py), since resolving them correctly
    requires a database session that this module deliberately never takes.
    Calling them here is a clear, actionable error, not silent wrong-answer
    calendar math."""
    with pytest.raises(ValidationError) as exc_info:
        resolve_date_preset(preset, now=NEAR_UTC_BOUNDARY)
    assert exc_info.value.field == "preset"
    assert "working_days_service" in exc_info.value.message


def test_month_to_date_starts_on_the_1st():
    start, end = resolve_date_preset("month_to_date", now=NEAR_UTC_BOUNDARY)
    assert start == dt.date(2026, 8, 1)
    assert end == EXPECTED_TODAY


def test_month_to_date_on_the_1st_is_a_single_day_range():
    first_of_month = dt.datetime(2026, 9, 1, 12, tzinfo=dt.timezone.utc)
    start, end = resolve_date_preset("month_to_date", now=first_of_month)
    assert start == end == dt.date(2026, 9, 1)


def test_naive_datetime_is_treated_as_utc_like_to_display_string():
    naive = dt.datetime(2026, 8, 22, 3, 30)  # no tzinfo
    assert resolve_date_preset("today", now=naive) == (EXPECTED_TODAY, EXPECTED_TODAY)


def test_unknown_preset_raises_validation_error():
    with pytest.raises(ValidationError) as exc_info:
        resolve_date_preset("bogus", now=NEAR_UTC_BOUNDARY)
    assert exc_info.value.field == "preset"


def test_defaults_to_the_real_current_instant_when_now_is_omitted():
    """Smoke test only - can't assert an exact date, just that it runs and
    returns a same-day range without needing an injected `now`."""
    start, end = resolve_date_preset("today")
    assert start == end


# ---------------------------------------------------------------------------
# this_week / last_week (calendar-only, added alongside This Week / Last Week
# Dashboard buttons) - 2026-08-17 is a Monday, 2026-08-19 a Wednesday,
# 2026-08-21 the Friday of that same week, 2026-08-23 a Sunday, 2026-08-24 the
# following Monday - same reference dates as tests/unit/test_working_days_
# service.py and tests/unit/test_brief_export_service.py.
# ---------------------------------------------------------------------------

MONDAY = dt.date(2026, 8, 17)
WEDNESDAY = dt.date(2026, 8, 19)
FRIDAY = dt.date(2026, 8, 21)
SUNDAY = dt.date(2026, 8, 23)
NEXT_MONDAY = dt.date(2026, 8, 24)
NEXT_FRIDAY = dt.date(2026, 8, 28)


def _now_on(d: dt.date) -> dt.datetime:
    """15:00 UTC is safely the same calendar day in DISPLAY_TIMEZONE
    (America/New_York) year-round - unlike NEAR_UTC_BOUNDARY above, these
    tests aren't targeting the tz-boundary edge case, just the Mon-Fri math."""
    return dt.datetime.combine(d, dt.time(15, 0), tzinfo=dt.timezone.utc)


def test_this_week_on_a_wednesday_is_monday_through_wednesday():
    assert resolve_date_preset("this_week", now=_now_on(WEDNESDAY)) == (MONDAY, WEDNESDAY)


def test_this_week_on_a_monday_is_a_single_day_range():
    start, end = resolve_date_preset("this_week", now=_now_on(MONDAY))
    assert start == end == MONDAY


def test_this_week_on_a_sunday_runs_monday_through_sunday_with_no_error():
    assert resolve_date_preset("this_week", now=_now_on(SUNDAY)) == (MONDAY, SUNDAY)


def test_last_week_on_a_monday_is_the_previous_mon_fri():
    assert resolve_date_preset("last_week", now=_now_on(NEXT_MONDAY)) == (MONDAY, FRIDAY)


def test_last_week_on_a_friday_is_the_same_previous_mon_fri_regardless_of_call_day():
    """last_week is anchored to the Monday of the CURRENT week, not the day
    it's called on - Monday and Friday of the same week must resolve
    identically."""
    on_monday = resolve_date_preset("last_week", now=_now_on(NEXT_MONDAY))
    on_friday = resolve_date_preset("last_week", now=_now_on(NEXT_FRIDAY))
    assert on_monday == on_friday == (MONDAY, FRIDAY)


def test_last_week_is_always_exactly_five_calendar_days():
    start, end = resolve_date_preset("last_week", now=_now_on(WEDNESDAY))
    assert (end - start).days == 4
    assert start.weekday() == 0
    assert end.weekday() == 4


def test_this_week_and_last_week_take_no_db_session():
    """Working Days Logic (Part C addendum) is explicit that this_week/
    last_week must NOT live in working_days_service (which takes a Session) -
    proven simply by every call above (and this one) passing no db argument
    and using no db_session fixture."""
    assert resolve_date_preset("this_week", now=_now_on(WEDNESDAY)) == (MONDAY, WEDNESDAY)
    assert resolve_date_preset("last_week", now=_now_on(NEXT_MONDAY)) == (MONDAY, FRIDAY)


@pytest.mark.parametrize("preset", ["this_week", "last_week"])
def test_this_week_and_last_week_are_in_calendar_only_presets(preset):
    """Regression guard: these two must be routed through resolve_date_preset
    (this module), never through working_days_service - see
    app/routers/reports.py get_date_preset's dispatch."""
    from app.timezone_utils import CALENDAR_ONLY_PRESETS, DATE_PRESETS

    assert preset in CALENDAR_ONLY_PRESETS
    assert preset in DATE_PRESETS


def test_working_day_presets_tuple_is_unchanged():
    """Regression guard: WORKING_DAY_PRESETS (app/services/working_days_service.py)
    must stay exactly ("yesterday", "last_7_days", "last_30_days") - this_week/
    last_week must never be added there."""
    from app.services.working_days_service import WORKING_DAY_PRESETS

    assert WORKING_DAY_PRESETS == ("yesterday", "last_7_days", "last_30_days")
