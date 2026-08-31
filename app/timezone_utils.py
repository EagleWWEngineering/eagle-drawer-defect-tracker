"""UTC storage, America/New_York display (PROJECT_SPEC.md section 5 / AGENTS.md)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.errors import ValidationError

_DISPLAY_TZ = ZoneInfo(get_settings().display_timezone)

# The full set of valid Dashboard date-preset names. "today" and "month_to_date"
# are calendar-only and resolved right here by resolve_date_preset(). "yesterday",
# "last_7_days", and "last_30_days" are working-day-aware (Working Days Logic,
# Part C addendum) and resolved by app/services/working_days_service.
# resolve_working_day_preset() instead - this module stays pure/DB-free, so it
# cannot compute those itself. See WORKING_DAY_PRESETS there.
DATE_PRESETS = ("today", "yesterday", "last_7_days", "last_30_days", "month_to_date")
CALENDAR_ONLY_PRESETS = ("today", "month_to_date")


def to_display_string(value: dt.datetime | None) -> str | None:
    """Format a UTC-aware datetime for on-screen/API display in the shop's timezone."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %I:%M %p %Z")


def today_in_display_timezone(now: dt.datetime | None = None) -> dt.date:
    """ "Today" IN DISPLAY_TIMEZONE, never UTC and never the caller's local time -
    the one calendar-boundary computation every preset (calendar or working-day)
    is built from.

    `now` is injectable (defaults to the real current instant) specifically so
    tests can exercise a moment where UTC has already rolled to the next
    calendar day but America/New_York has not (e.g. ~8pm-midnight Eastern) -
    a naive implementation using UTC (or a server/browser's local time) fails
    exactly there. A naive datetime is treated as UTC, matching to_display_string.
    """
    effective_now = now if now is not None else dt.datetime.now(dt.timezone.utc)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=dt.timezone.utc)
    return effective_now.astimezone(_DISPLAY_TZ).date()


def resolve_date_preset(preset: str, *, now: dt.datetime | None = None) -> tuple[dt.date, dt.date]:
    """(start_date, end_date), both inclusive, for one of the Dashboard's
    CALENDAR_ONLY_PRESETS (PROJECT_SPEC.md Phase 6 addendum 5a) - computed
    against today_in_display_timezone(). This is the one place that logic lives:
    the dashboard calls GET /api/v1/reports/date-preset (app/routers/reports.py)
    rather than re-deriving these boundaries in JavaScript, so there is only
    ever one implementation to get right/test.

    Preset definitions:
      - "today": the current date in DISPLAY_TIMEZONE.
      - "month_to_date": the 1st of the current month through today.

    "yesterday", "last_7_days", and "last_30_days" are NOT handled here as of the
    Working Days Logic (Part C) addendum - they're working-day-aware and require
    a database session, which this module deliberately never takes. Callers for
    those go through app/services/working_days_service.resolve_working_day_preset
    instead (app/routers/reports.py's get_date_preset does exactly this
    dispatch). Passing one of them here raises ValidationError pointing at that.

    Raises ValidationError for any other/unknown preset name.
    """
    today = today_in_display_timezone(now)

    if preset == "today":
        return today, today
    if preset == "month_to_date":
        return today.replace(day=1), today
    if preset in DATE_PRESETS:
        raise ValidationError(
            f"Preset {preset!r} is working-day-aware - call "
            "working_days_service.resolve_working_day_preset() instead of "
            "timezone_utils.resolve_date_preset() directly.",
            field="preset",
        )
    raise ValidationError(
        f"Unknown date preset {preset!r} - expected one of {DATE_PRESETS}.", field="preset"
    )
