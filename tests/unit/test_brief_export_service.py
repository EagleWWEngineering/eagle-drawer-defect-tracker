"""Unit tests for app/services/brief_export_service.py (Brief Export, Part A).

2026-08-17 is a Monday, 2026-08-21 is the Friday of that same week, 2026-08-24
is the following Monday - the same reference dates
tests/unit/test_working_days_service.py uses, chosen to land on real
weekdays/weekends without relying on any timezone/display logic.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import event

from app.models import DailyProductionSummary
from app.services import brief_export_service, defect_service, metrics_service, schedule_service
from app.services import working_days_service as wds
from app.services.brief_export_service import build_last_production_day, build_week_summary

MONDAY = dt.date(2026, 8, 17)
TUESDAY = dt.date(2026, 8, 18)
WEDNESDAY = dt.date(2026, 8, 19)
THURSDAY = dt.date(2026, 8, 20)
FRIDAY = dt.date(2026, 8, 21)
SATURDAY = dt.date(2026, 8, 22)
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


def test_last_production_day_cases_and_defect_events_for_that_single_date(
    db_session, stations, categories
):
    """Part 1: cases/defect_events reflect DefectCase rows on the resolved date
    only - via the same _case_and_event_counts helper build_week_summary
    uses."""
    _add_summary(db_session, FRIDAY, drawers_inspected=390)
    _make_case(
        db_session,
        stations,
        categories,
        production_date=FRIDAY,
        wo="WO-1",
        category_counts={"Sanding / Surface": 3, "Dado / Bottom Groove": 1},
    )
    # A case on a different date must not leak into Friday's figures.
    _make_case(
        db_session,
        stations,
        categories,
        production_date=THURSDAY,
        wo="WO-2",
        category_counts={"Bad Wood / Material": 100},
    )

    result = build_last_production_day(db_session, NEXT_MONDAY)

    assert result["date"] == FRIDAY
    assert result["cases"] == 1
    assert result["defect_events"] == 4


def test_last_production_day_no_cases_is_real_zero_not_none(db_session):
    """A day with genuinely no defect cases is cases=0/defect_events=0 - real,
    verified zeros, never null (unlike the un-entered inspection count)."""
    result = build_last_production_day(db_session, NEXT_MONDAY)

    assert result["cases"] == 0
    assert result["cases"] is not None
    assert result["defect_events"] == 0
    assert result["defect_events"] is not None


def test_last_production_day_cases_decoupled_from_entered(db_session, stations, categories):
    """entered=False (no daily_production_summaries row) must not imply
    cases/defect_events are null or zero - a case can be logged before the
    Daily Summary form is ever filled in for that date."""
    _make_case(
        db_session,
        stations,
        categories,
        production_date=FRIDAY,
        wo="WO-1",
        category_counts={"Sanding / Surface": 2},
    )

    result = build_last_production_day(db_session, NEXT_MONDAY)

    assert result["entered"] is False
    assert result["inspected"] is None
    assert result["cases"] == 1
    assert result["defect_events"] == 2


def test_last_production_day_single_category_quantity_five_is_one_case_five_events(
    db_session, stations, categories
):
    """Pins the definition Part A reconciled to: affected_drawer_quantity=5 on
    one DefectItem is 5 defect_events but still 1 case (one case = one
    defective drawer, regardless of how many events it carries)."""
    _make_case(
        db_session,
        stations,
        categories,
        production_date=FRIDAY,
        wo="WO-1",
        category_counts={"Sanding / Surface": 5},
    )

    result = build_last_production_day(db_session, NEXT_MONDAY)

    assert result["cases"] == 1
    assert result["defect_events"] == 5


def test_last_production_day_excludes_soft_deleted_cases(db_session, stations, categories):
    case = _make_case(
        db_session,
        stations,
        categories,
        production_date=FRIDAY,
        wo="WO-1",
        category_counts={"Sanding / Surface": 5},
    )
    defect_service.soft_delete_case(db_session, case)

    result = build_last_production_day(db_session, NEXT_MONDAY)

    assert result["cases"] == 0
    assert result["defect_events"] == 0


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


# ---------------------------------------------------------------------------
# week: days (Part 2 addendum - scheduled-vs-inspected bar chart)
# ---------------------------------------------------------------------------


def test_week_days_one_entry_per_working_day_ascending_order(db_session):
    for d, n in [(MONDAY, 380), (TUESDAY, 390), (WEDNESDAY, 400), (THURSDAY, 410), (FRIDAY, 420)]:
        _add_summary(db_session, d, drawers_inspected=n)

    result = build_week_summary(db_session, FRIDAY)

    assert [day["date"] for day in result["days"]] == [MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY]
    assert [day["inspected"] for day in result["days"]] == [380, 390, 400, 410, 420]
    assert all(day["entered"] for day in result["days"])


def test_week_days_no_summary_row_entered_false_inspected_none_not_zero(db_session):
    _add_summary(db_session, MONDAY, drawers_inspected=380)
    # Tuesday-Friday: no daily_production_summaries rows at all.

    result = build_week_summary(db_session, FRIDAY)

    by_date = {day["date"]: day for day in result["days"]}
    assert by_date[TUESDAY]["entered"] is False
    assert by_date[TUESDAY]["inspected"] is None
    assert by_date[TUESDAY]["inspected"] != 0


def test_week_days_two_shifts_on_one_date_summed(db_session):
    _add_summary(db_session, MONDAY, drawers_inspected=200, shift="Day")
    _add_summary(db_session, MONDAY, drawers_inspected=190, shift="Night")

    result = build_week_summary(db_session, TUESDAY)  # week_to_date: Mon-Tue

    monday_entry = next(day for day in result["days"] if day["date"] == MONDAY)
    assert monday_entry["entered"] is True
    assert monday_entry["inspected"] == 390


def test_week_days_weekend_date_excluded_even_with_defect_cases(db_session, stations, categories):
    """_week_range itself never spans a weekend (always Mon-Fri), so this
    exercises _build_week_days directly with a range that does - the helper
    must still apply the real working-day rule, not just trust its input."""
    _make_case(
        db_session,
        stations,
        categories,
        production_date=SATURDAY,
        wo="WO-SAT",
        category_counts={"Sanding / Surface": 2},
    )
    start, end = FRIDAY, SATURDAY + dt.timedelta(days=1)  # Fri-Sun
    items = metrics_service.filtered_defect_items_query(
        db_session, start_date=start, end_date=end
    ).all()

    days = brief_export_service._build_week_days(db_session, items, start, end)

    assert SATURDAY not in [day["date"] for day in days]


def test_week_days_overtime_saturday_included_via_manual_schedule(db_session):
    """The existing escape hatch (a source="manual" schedule row) must still
    make an overtime Saturday a working day here, exactly as it does
    everywhere else via working_days_service."""
    schedule_service.upsert_schedule(
        db_session, production_date=SATURDAY, drawers_scheduled=40, source="manual"
    )
    _add_summary(db_session, SATURDAY, drawers_inspected=38)
    start, end = FRIDAY, SATURDAY + dt.timedelta(days=1)
    items = metrics_service.filtered_defect_items_query(
        db_session, start_date=start, end_date=end
    ).all()

    days = brief_export_service._build_week_days(db_session, items, start, end)

    by_date = {day["date"]: day for day in days}
    assert SATURDAY in by_date
    assert by_date[SATURDAY]["entered"] is True
    assert by_date[SATURDAY]["inspected"] == 38


def test_week_days_cases_sum_matches_week_cases(db_session, stations, categories):
    _make_case(
        db_session,
        stations,
        categories,
        production_date=MONDAY,
        wo="WO-1",
        category_counts={"Sanding / Surface": 14, "Dado / Bottom Groove": 9},
    )
    _make_case(
        db_session,
        stations,
        categories,
        production_date=WEDNESDAY,
        wo="WO-2",
        category_counts={"Bad Wood / Material": 6},
    )
    _make_case(
        db_session,
        stations,
        categories,
        production_date=WEDNESDAY,
        wo="WO-3",
        category_counts={"Bottom Panel": 4},
    )

    result = build_week_summary(db_session, WEDNESDAY)

    assert sum(day["cases"] for day in result["days"]) == result["cases"]
    assert result["cases"] == 3


def test_week_days_cases_decoupled_from_entered(db_session, stations, categories):
    """A day with logged defect cases but no Daily Summary form entry yet must
    be entered=False/inspected=None while still carrying its real, nonzero
    cases count - the same decoupling build_last_production_day already
    applies to a single date."""
    _make_case(
        db_session,
        stations,
        categories,
        production_date=THURSDAY,
        wo="WO-1",
        category_counts={"Sanding / Surface": 2},
    )

    result = build_week_summary(db_session, FRIDAY)

    thursday_entry = next(day for day in result["days"] if day["date"] == THURSDAY)
    assert thursday_entry["entered"] is False
    assert thursday_entry["inspected"] is None
    assert thursday_entry["cases"] == 1


def test_week_days_empty_when_no_working_days_in_range(db_session):
    for d in (MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY):
        schedule_service.upsert_schedule(
            db_session, production_date=d, drawers_scheduled=0, source="sync"
        )
    # explicit scheduled-0 + zero-inspected on every weekday of the range ->
    # none of them count as working days (working_days_service's Mon-Fri
    # fallback rule), so the range has no working days at all.

    result = build_week_summary(db_session, FRIDAY)

    assert result["days"] == []


def test_week_days_query_count_does_not_scale_with_range_length(db_session):
    """The days breakdown must fetch its bulk queries once per call, not once
    per day - a 4-week range must cost exactly the same as a 5-day week."""

    def _count_queries(start: dt.date, end: dt.date) -> int:
        items = metrics_service.filtered_defect_items_query(
            db_session, start_date=start, end_date=end
        ).all()
        calls: list[object] = []

        def _listener(*args, **kwargs):
            calls.append(1)

        bind = db_session.get_bind()
        event.listen(bind, "before_cursor_execute", _listener)
        try:
            brief_export_service._build_week_days(db_session, items, start, end)
        finally:
            event.remove(bind, "before_cursor_execute", _listener)
        return len(calls)

    short_week_count = _count_queries(MONDAY, FRIDAY)  # 5 days
    four_week_count = _count_queries(MONDAY, MONDAY + dt.timedelta(days=27))  # 28 days

    assert short_week_count == four_week_count
    # working_day_set's own 2 bulk queries + this function's 1 grouped
    # inspected-sum query - never one query per day.
    assert short_week_count <= 3
