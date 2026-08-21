"""UTC storage, America/New_York display (PROJECT_SPEC.md section 5 / AGENTS.md)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.errors import ValidationError

_DISPLAY_TZ = ZoneInfo(get_settings().display_timezone)

DATE_PRESETS = ("today", "yesterday", "last_7_days", "last_30_days", "month_to_date")


def to_display_string(value: dt.datetime | None) -> str | None:
    """Format a UTC-aware datetime for on-screen/API display in the shop's timezone."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %I:%M %p %Z")


def resolve_date_preset(preset: str, *, now: dt.datetime | None = None) -> tuple[dt.date, dt.date]:
    """(start_date, end_date), both inclusive, for one of the Dashboard's date
    presets (PROJECT_SPEC.md Phase 6 addendum 5a) - computed against "today" IN
    DISPLAY_TIMEZONE, never UTC and never the caller's local time. This is the one
    place that logic lives: the dashboard calls GET /api/v1/reports/date-preset
    (app/routers/reports.py) rather than re-deriving these boundaries in
    JavaScript, so there is only ever one implementation to get right/test.

    `now` is injectable (defaults to the real current instant) specifically so
    tests can exercise a moment where UTC has already rolled to the next
    calendar day but America/New_York has not (e.g. ~8pm-midnight Eastern) -
    a naive implementation using UTC (or a server/browser's local time) fails
    exactly there. A naive datetime is treated as UTC, matching to_display_string.

    Preset definitions:
      - "today": the current date in DISPLAY_TIMEZONE.
      - "yesterday": the previous calendar day.
      - "last_7_days": trailing 7 days, including today.
      - "last_30_days": trailing 30 days, including today.
      - "month_to_date": the 1st of the current month through today.

    Raises ValidationError for any other preset name.
    """
    effective_now = now if now is not None else dt.datetime.now(dt.timezone.utc)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=dt.timezone.utc)
    today = effective_now.astimezone(_DISPLAY_TZ).date()

    if preset == "today":
        return today, today
    if preset == "yesterday":
        yesterday = today - dt.timedelta(days=1)
        return yesterday, yesterday
    if preset == "last_7_days":
        return today - dt.timedelta(days=6), today
    if preset == "last_30_days":
        return today - dt.timedelta(days=29), today
    if preset == "month_to_date":
        return today.replace(day=1), today
    raise ValidationError(
        f"Unknown date preset {preset!r} - expected one of {DATE_PRESETS}.", field="preset"
    )
