"""Working-day awareness (Part C addendum): whether a calendar date counts as a
working day at Eagle - Monday-Friday, minus holidays/shutdowns, plus the rare
overtime Saturday. This is the SINGLE place the definition lives; no other module
reimplements it (not even a bare `weekday() < 5` check) - see CLAUDE.md and the
Working Days Logic prompt for the full rationale.

Definition (all three are "or"s - any one of them makes a date a working day):
  - it has a daily_schedules row with drawers_scheduled > 0, or
  - it has a daily_production_summaries row (any shift, summed) with
    drawers_inspected > 0, or
  - it is Mon-Fri and does NOT have both a scheduled 0 and zero inspected.

That third branch is what makes a missing schedule row (a failed scrape) never
remove a weekday, while an explicit scheduled-0 + zero-inspected (a holiday the
production brief actually ran for) does. It's also what makes a brand-new
database with no rows at all fall back to plain Mon-Fri.

The first two branches are what let a manually-entered overtime Saturday (source=
"manual" schedule row, or inspections logged that day) become a working day with
no special-case code anywhere - see app/services/schedule_service.py's
manual-wins rule.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.errors import ServiceError, ValidationError
from app.models import DailyProductionSummary, DailySchedule

# Cap for walk_back_working_days' backward search - guards against an infinite
# loop on a pathological all-weekend/all-holiday stretch. 60 calendar days is
# ~8-9 weeks, far more than any realistic shutdown.
_MAX_WALK_BACK_DAYS = 60

# The Dashboard date presets that need working-day awareness (as opposed to
# app/timezone_utils.py's CALENDAR_ONLY_PRESETS) - see resolve_working_day_preset
# below and app/timezone_utils.py DATE_PRESETS for the full preset name list.
WORKING_DAY_PRESETS = ("yesterday", "last_7_days", "last_30_days")


def _is_working_day_from_values(day: dt.date, scheduled: int | None, inspected: int) -> bool:
    """The definition, applied to one date's already-fetched figures. Kept as its
    own function so working_day_set (bulk, 2 queries) and is_working_day (thin
    wrapper below) can never drift apart from each other."""
    if scheduled is not None and scheduled > 0:
        return True
    if inspected > 0:
        return True
    if day.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    # Mon-Fri fallback: a working day unless the brief explicitly recorded a
    # scheduled zero AND nothing was inspected (a real holiday/shutdown). A
    # missing schedule row (scheduled is None) never satisfies "scheduled == 0",
    # so a failed scrape leaves the weekday counted as a working day.
    return not (scheduled == 0 and inspected == 0)


def working_day_set(db: Session, start: dt.date, end: dt.date) -> set[dt.date]:
    """Every working day in [start, end] inclusive, per the module definition.

    Issues at most two queries total (one against daily_schedules, one against
    daily_production_summaries, both grouped/aggregated across the whole range) -
    never one query per day, so a 30-day preset stays two round trips instead of
    thirty.
    """
    if start > end:
        return set()

    scheduled_by_date: dict[dt.date, int] = {
        row.production_date: row.drawers_scheduled
        for row in db.query(DailySchedule.production_date, DailySchedule.drawers_scheduled)
        .filter(DailySchedule.production_date >= start, DailySchedule.production_date <= end)
        .all()
    }

    inspected_by_date: dict[dt.date, int] = {
        production_date: total or 0
        for production_date, total in db.query(
            DailyProductionSummary.production_date,
            func.sum(DailyProductionSummary.drawers_inspected),
        )
        .filter(
            DailyProductionSummary.production_date >= start,
            DailyProductionSummary.production_date <= end,
        )
        .group_by(DailyProductionSummary.production_date)
        .all()
    }

    working_days: set[dt.date] = set()
    day = start
    while day <= end:
        scheduled = scheduled_by_date.get(day)
        inspected = inspected_by_date.get(day, 0)
        if _is_working_day_from_values(day, scheduled, inspected):
            working_days.add(day)
        day += dt.timedelta(days=1)
    return working_days


def is_working_day(db: Session, d: dt.date) -> bool:
    """The definition, for one date. Implemented in terms of working_day_set (a
    same-date range) rather than duplicated, so there is exactly one code path."""
    return d in working_day_set(db, d, d)


def walk_back_working_days(db: Session, from_date: dt.date, n: int = 1) -> dt.date:
    """Step back `n` working days from (but not including) `from_date` - e.g. the
    Yesterday preset on a Monday walks back 1 working day and lands on the
    previous Friday (or Thursday, if Friday was a holiday).

    Fetches the whole _MAX_WALK_BACK_DAYS-day lookback window in one
    working_day_set call (two queries total, regardless of `n`), then walks it
    backward in memory - not one is_working_day() call per candidate day.

    Raises ServiceError if fewer than `n` working days are found within the
    lookback window, rather than spinning forever on an all-weekend/all-holiday
    stretch.
    """
    if n <= 0:
        return from_date

    window_start = from_date - dt.timedelta(days=_MAX_WALK_BACK_DAYS)
    window_end = from_date - dt.timedelta(days=1)
    candidates = working_day_set(db, window_start, window_end)

    remaining = n
    day = window_end
    while day >= window_start:
        if day in candidates:
            remaining -= 1
            if remaining == 0:
                return day
        day -= dt.timedelta(days=1)

    raise ServiceError(
        f"Could not find {n} working day(s) within {_MAX_WALK_BACK_DAYS} calendar "
        f"days before {from_date.isoformat()}."
    )


def previous_working_day(db: Session, d: dt.date) -> dt.date:
    """Thin alias for walk_back_working_days(db, d, 1) - reads better at call
    sites that only ever want "the one before this"."""
    return walk_back_working_days(db, d, 1)


def resolve_working_day_preset(
    db: Session, preset: str, *, today: dt.date
) -> tuple[dt.date, dt.date]:
    """(start_date, end_date), both inclusive, for one of WORKING_DAY_PRESETS.
    `today` is the caller-resolved "today in DISPLAY_TIMEZONE" (see
    app/timezone_utils.py today_in_display_timezone()) - this module never
    computes timezone boundaries itself, only working-day ones.

    Preset definitions:
      - "yesterday": the previous WORKING day (Monday -> Friday; Thursday if
        Friday was a holiday).
      - "last_7_days" / "last_30_days": the trailing 7 / 30 WORKING days,
        through today - end_date is always `today` (matching how the old
        calendar presets always ran through today), start_date is walked back
        far enough that exactly 7 (or 30) working days fall in [start, today],
        counting today itself if today is a working day.

    Raises ValidationError for any other preset name (including the
    calendar-only ones - those go through timezone_utils.resolve_date_preset).
    """
    if preset == "yesterday":
        d = previous_working_day(db, today)
        return d, d
    if preset in ("last_7_days", "last_30_days"):
        n = 7 if preset == "last_7_days" else 30
        # today counts as one of the n working days if it is one itself.
        n_before_today = n - 1 if is_working_day(db, today) else n
        start = walk_back_working_days(db, today, n_before_today)
        return start, today
    raise ValidationError(
        f"Unknown working-day preset {preset!r} - expected one of {WORKING_DAY_PRESETS}.",
        field="preset",
    )
