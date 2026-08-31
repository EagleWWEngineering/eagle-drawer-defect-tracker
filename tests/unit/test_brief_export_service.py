"""Unit tests for app/services/brief_export_service.py (Brief Export, Part A).

2026-08-17 is a Monday, 2026-08-21 is the Friday of that same week, 2026-08-24
is the following Monday - the same reference dates
tests/unit/test_working_days_service.py uses, chosen to land on real
weekdays/weekends without relying on any timezone/display logic.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.models import DailyProductionSummary
from app.services import brief_export_service, defect_service, schedule_service
from app.services import working_days_service as wds
from app.services.brief_export_service import build_last_production_day, build_week_summary

MONDAY = dt.date(2026, 8, 17)
WEDNESDAY = dt.date(2026, 8, 19)
THURSDAY = dt.date(2026, 8, 20)
FRIDAY = dt.date(2026, 8, 21)
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


def _make_case(db, stations, categories, *, production_date, wo, category_counts):
    """One DefectCase on `production_date` with one DefectItem per
    (category name -> affected_drawer_quantity) pair in `category_counts`."""
    return defect_service.create_defect_case(
        db,
        production_date=production_date,
        detected_at=dt.datetime.combine(production_date, dt.time(9, 0), tzinfo=dt.timezone.utc),
        work_order_number=wo,
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[
            {"defect_category_id": categories[name].id, "affected_drawer_quantity": qty}
            for name, qty in category_counts.items()
        ],
    )


# ---------------------------------------------------------------------------
# last_production_day
# ---------------------------------------------------------------------------


def test_last_production_day_monday_asof_is_prior_friday(db_session):
    _add_summary(db_session, FRIDAY, drawers_inspected=390)
    schedule_service.upsert_schedule(
        db_session, production_date=FRIDAY, drawers_scheduled=406, source="sync"
    )

    result = build_last_production_day(db_session, NEXT_MONDAY)

    assert result["date"] == FRIDAY
    assert result["entered"] is True
    assert result["inspected"] == 390
    assert result["scheduled_per_tracker"] == 406


def test_last_production_day_friday_holiday_walks_back_to_thursday(db_session):
    # Friday: brief ran, scheduled 0, nothing inspected - a recorded holiday.
    schedule_service.upsert_schedule(
        db_session, production_date=FRIDAY, drawers_scheduled=0, source="sync"
    )
    _add_summary(db_session, THURSDAY, drawers_inspected=350)
    schedule_service.upsert_schedule(
        db_session, production_date=THURSDAY, drawers_scheduled=380, source="sync"
    )

    result = build_last_production_day(db_session, NEXT_MONDAY)

    assert result["date"] == THURSDAY
    assert result["entered"] is True
    assert result["inspected"] == 350


def test_last_production_day_no_summary_row_entered_false_inspected_none(db_session):
    """Brand-new DB, nothing entered yet for the resolved date (Friday, per the
    Mon-Fri fallback - see working_days_service). Must be False/None, never 0 -
    "nobody entered it yet" is not the same fact as "we inspected zero"."""
    result = build_last_production_day(db_session, NEXT_MONDAY)

    assert result["date"] == FRIDAY
    assert result["entered"] is False
    assert result["inspected"] is None
    assert result["inspected"] != 0
    assert result["scheduled_per_tracker"] is None


def test_last_production_day_two_shifts_summed(db_session):
    _add_summary(db_session, FRIDAY, drawers_inspected=200, shift="Day")
    _add_summary(db_session, FRIDAY, drawers_inspected=190, shift="Night")

    result = build_last_production_day(db_session, NEXT_MONDAY)

    assert result["entered"] is True
    assert result["inspected"] == 390


def test_last_production_day_null_when_no_working_day_found_in_lookback_window(db_session):
    """Every day in the 60-day lookback window is an explicit
    scheduled-0/zero-inspected weekday or a weekend - previous_working_day
    raises ServiceError; this must surface as None, never an exception."""
    d = NEXT_MONDAY
    for _ in range(wds._MAX_WALK_BACK_DAYS + 5):
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            schedule_service.upsert_schedule(
                db_session, production_date=d, drawers_scheduled=0, source="sync"
            )

    result = build_last_production_day(db_session, NEXT_MONDAY)

    assert result is None


# ---------------------------------------------------------------------------
# week: basis + range
# ---------------------------------------------------------------------------


def test_week_monday_asof_is_prior_full_week(db_session):
    result = build_week_summary(db_session, NEXT_MONDAY)

    assert result["basis"] == "prior_full_week"
    assert result["start"] == MONDAY
    assert result["end"] == FRIDAY


def test_week_wednesday_asof_is_week_to_date_ending_wednesday(db_session):
    result = build_week_summary(db_session, WEDNESDAY)

    assert result["basis"] == "week_to_date"
    assert result["start"] == MONDAY
    assert result["end"] == WEDNESDAY


def test_week_saturday_asof_defensively_treated_as_prior_full_week(db_session):
    saturday = dt.date(2026, 8, 22)
    result = build_week_summary(db_session, saturday)

    assert result["basis"] == "prior_full_week"
    assert result["start"] == MONDAY
    assert result["end"] == FRIDAY


# ---------------------------------------------------------------------------
# week: Pareto / top-3 + other
# ---------------------------------------------------------------------------


def test_week_empty_range_returns_zeros_not_nulls(db_session):
    result = build_week_summary(db_session, WEDNESDAY)

    assert result["cases"] == 0
    assert result["defect_events"] == 0
    assert result["top_categories"] == []
    assert result["other_count"] == 0


def test_week_top3_and_other_sum_to_defect_events(db_session, stations, categories):
    _make_case(
        db_session,
        stations,
        categories,
        production_date=MONDAY,
        wo="WO-1",
        category_counts={
            "Sanding / Surface": 14,
            "Dado / Bottom Groove": 9,
        },
    )
    _make_case(
        db_session,
        stations,
        categories,
        production_date=WEDNESDAY,
        wo="WO-2",
        category_counts={
            "Bad Wood / Material": 6,
            "Bottom Panel": 4,
            "Assembly / Joint / Glue / Staple": 3,
        },
    )

    result = build_week_summary(db_session, WEDNESDAY)

    assert result["defect_events"] == 14 + 9 + 6 + 4 + 3
    assert len(result["top_categories"]) == 3
    top_sum = sum(c["count"] for c in result["top_categories"])
    assert top_sum + result["other_count"] == result["defect_events"]
    assert result["cases"] == 2


def test_week_fewer_than_3_categories_shorter_list_other_zero(db_session, stations, categories):
    _make_case(
        db_session,
        stations,
        categories,
        production_date=MONDAY,
        wo="WO-1",
        category_counts={"Sanding / Surface": 5, "Dado / Bottom Groove": 2},
    )

    result = build_week_summary(db_session, WEDNESDAY)

    assert len(result["top_categories"]) == 2
    assert result["other_count"] == 0
    assert result["defect_events"] == 7


def test_week_deterministic_tiebreak_at_third_position(db_session, stations, categories):
    """Bad Wood/Material=10 and Bottom Panel=8 clearly take the first two
    slots. Cutting/Incorrect Dimension and Dado/Bottom Groove tie at 5 for the
    3rd slot - alphabetically "Cutting..." sorts before "Dado...", so it must
    win the slot and "Dado..." must fall into other_count, deterministically
    (never by insertion order)."""
    _make_case(
        db_session,
        stations,
        categories,
        production_date=MONDAY,
        wo="WO-1",
        category_counts={
            "Bad Wood / Material": 10,
            "Bottom Panel": 8,
            "Dado / Bottom Groove": 5,
            "Cutting / Incorrect Dimension": 5,
        },
    )

    result = build_week_summary(db_session, WEDNESDAY)

    names = [c["name"] for c in result["top_categories"]]
    assert names == ["Bad Wood / Material", "Bottom Panel", "Cutting / Incorrect Dimension"]
    assert result["other_count"] == 5


# ---------------------------------------------------------------------------
# product validation
# ---------------------------------------------------------------------------


def test_validate_product_accepts_drawers():
    brief_export_service.validate_product("drawers")  # must not raise


def test_validate_product_rejects_anything_else():
    from app.errors import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        brief_export_service.validate_product("doors")
    assert exc_info.value.field == "product"
