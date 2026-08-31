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
