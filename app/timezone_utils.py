"""UTC storage, America/New_York display (PROJECT_SPEC.md section 5 / AGENTS.md)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from app.config import get_settings

_DISPLAY_TZ = ZoneInfo(get_settings().display_timezone)


def to_display_string(value: dt.datetime | None) -> str | None:
    """Format a UTC-aware datetime for on-screen/API display in the shop's timezone."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %I:%M %p %Z")
