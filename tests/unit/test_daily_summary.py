"""PROJECT_SPEC.md section 2.3: daily production summary hard rule + soft warnings."""

from __future__ import annotations

import datetime as dt
import decimal

import pytest

from app.errors import ValidationError
from app.services import settings_service
from app.services.defect_service import (
    count_rework_cases_by_date,
    create_defect_case,
    get_daily_summary,
    suggested_daily_counts,
    upsert_daily_summary,
)


def test_rejected_exceeding_inspected_is_a_hard_block(db_session, today):
    with pytest.raises(ValidationError):
        upsert_daily_summary(
            db_session,
            production_date=today,
            shift="Day",
            drawers_inspected=10,
            drawers_rejected_unique=11,
            drawers_reworked=0,
            drawers_scrapped=0,
            notes=None,
        )


def test_rework_exceeding_rejected_is_a_soft_warning_requiring_note(db_session, today):
    # No note -> rejected because rework > rejected_unique is unusual and unexplained.
    with pytest.raises(ValidationError):
        upsert_daily_summary(
            db_session,
            production_date=today,
            shift="Day",
            drawers_inspected=50,
            drawers_rejected_unique=0,
            drawers_reworked=5,
            drawers_scrapped=0,
            notes=None,
        )

    # With a note explaining it (e.g. rework of a prior day's rejections), it's allowed.
    row, warnings = upsert_daily_summary(
        db_session,
        production_date=today,
        shift="Day",
        drawers_inspected=50,
        drawers_rejected_unique=0,
        drawers_reworked=5,
        drawers_scrapped=0,
        notes="Reworked drawers rejected yesterday.",
    )
    assert row.drawers_reworked == 5
    assert len(warnings) >= 1


def test_normal_entry_has_no_warnings(db_session, today):
    row, warnings = upsert_daily_summary(
        db_session,
        production_date=today,
        shift="Day",
        drawers_inspected=100,
        drawers_rejected_unique=10,
        drawers_reworked=7,
        drawers_scrapped=2,
        notes=None,
    )
    assert warnings == []
    assert row.drawers_inspected == 100


def test_upsert_updates_existing_row_for_same_date_and_shift(db_session, today):
    upsert_daily_summary(
        db_session,
        production_date=today,
        shift="Day",
        drawers_inspected=100,
        drawers_rejected_unique=10,
        drawers_reworked=5,
        drawers_scrapped=2,
        notes=None,
    )
    row, _ = upsert_daily_summary(
        db_session,
        production_date=today,
        shift="Day",
        drawers_inspected=120,
        drawers_rejected_unique=12,
        drawers_reworked=6,
        drawers_scrapped=3,
        notes=None,
    )
    from app.models import DailyProductionSummary

    all_rows = db_session.query(DailyProductionSummary).all()
    assert len(all_rows) == 1, "same production_date+shift must update, not duplicate"
    assert row.drawers_inspected == 120


def test_upsert_stamps_current_rate_at_save_time(db_session, today):
    row, _ = upsert_daily_summary(
        db_session,
        production_date=today,
        shift="Day",
        drawers_inspected=100,
        drawers_rejected_unique=10,
        drawers_reworked=5,
        drawers_scrapped=2,
        notes=None,
    )
    assert row.cost_per_drawer_at_time == decimal.Decimal("35.00")  # seeded default


def test_changing_rate_does_not_alter_already_saved_historical_summary(db_session, today):
    """PROJECT_SPEC section on cost tracking: historical data keeps the rate that
    was active at the time - it must never change when the Admin rate changes."""
    yesterday = today - dt.timedelta(days=1)
    old_row, _ = upsert_daily_summary(
        db_session,
        production_date=yesterday,
        shift="Day",
        drawers_inspected=100,
        drawers_rejected_unique=10,
        drawers_reworked=5,
        drawers_scrapped=2,
        notes=None,
    )
    assert old_row.cost_per_drawer_at_time == decimal.Decimal("35.00")

    settings_service.set_cost_per_drawer(db_session, decimal.Decimal("50.00"))

    new_row, _ = upsert_daily_summary(
        db_session,
        production_date=today,
        shift="Day",
        drawers_inspected=80,
        drawers_rejected_unique=8,
        drawers_reworked=4,
        drawers_scrapped=1,
        notes=None,
    )
    assert new_row.cost_per_drawer_at_time == decimal.Decimal("50.00")

    db_session.refresh(old_row)
    assert old_row.cost_per_drawer_at_time == decimal.Decimal(
        "35.00"
    ), "yesterday's summary must keep its original rate after the rate changes"


# ---------------------------------------------------------------------------
# Scrap removal (docs/PROJECT_SPEC_PHASE4.md): drawers_scrapped is no longer a
# Daily Summary form field. Omitting it (None) must never silently zero out an
# already-saved legacy value.
# ---------------------------------------------------------------------------


def test_omitting_scrapped_defaults_a_new_row_to_zero(db_session, today):
    row, _ = upsert_daily_summary(
        db_session,
        production_date=today,
        shift="Day",
        drawers_inspected=100,
        drawers_rejected_unique=10,
        drawers_reworked=5,
        drawers_scrapped=None,
        notes=None,
    )
    assert row.drawers_scrapped == 0


def test_omitting_scrapped_on_resave_preserves_existing_value(db_session, today):
    upsert_daily_summary(
        db_session,
        production_date=today,
        shift="Day",
        drawers_inspected=100,
        drawers_rejected_unique=10,
        drawers_reworked=5,
        drawers_scrapped=3,
        notes=None,
    )
    # The (new) Daily Summary form re-saves this date/shift without ever sending a
    # scrapped value at all - the historical 3 must survive, not become 0.
    row, _ = upsert_daily_summary(
        db_session,
        production_date=today,
        shift="Day",
        drawers_inspected=120,
        drawers_rejected_unique=12,
        drawers_reworked=6,
        drawers_scrapped=None,
        notes=None,
    )
    assert row.drawers_scrapped == 3


# ---------------------------------------------------------------------------
# PROJECT_SPEC_PHASE7.md: drawers_reworked also left the Daily Summary form
# (Rework Rate is now computed from defect cases, not this hand-entered count).
# Same None-preserving rule as drawers_scrapped above.
# ---------------------------------------------------------------------------


def test_omitting_reworked_defaults_a_new_row_to_zero(db_session, today):
    row, _ = upsert_daily_summary(
        db_session,
        production_date=today,
        shift="Day",
        drawers_inspected=100,
        drawers_rejected_unique=10,
        drawers_reworked=None,
        drawers_scrapped=None,
        notes=None,
    )
    assert row.drawers_reworked == 0


def test_omitting_reworked_on_resave_preserves_existing_value(db_session, today):
    upsert_daily_summary(
        db_session,
        production_date=today,
        shift="Day",
        drawers_inspected=100,
        drawers_rejected_unique=10,
        drawers_reworked=5,
        drawers_scrapped=None,
        notes=None,
    )
    # The (new) Daily Summary form re-saves this date/shift without ever sending a
    # reworked value at all - the historical 5 must survive, not become 0.
    row, _ = upsert_daily_summary(
        db_session,
        production_date=today,
        shift="Day",
        drawers_inspected=120,
        drawers_rejected_unique=12,
        drawers_reworked=None,
        drawers_scrapped=None,
        notes=None,
    )
    assert row.drawers_reworked == 5


# ---------------------------------------------------------------------------
# Auto-calculation of Rejected/Reworked from real DefectCase data
# (docs/PROJECT_SPEC_PHASE4.md "Scrap removal" / auto-calculation).
# ---------------------------------------------------------------------------


def _detected_at(today: dt.date) -> dt.datetime:
    return dt.datetime(today.year, today.month, today.day, 9, 0, tzinfo=dt.timezone.utc)


def test_get_daily_summary_returns_none_when_nothing_saved(db_session, today):
    assert get_daily_summary(db_session, today, "Day") is None


def test_get_daily_summary_returns_the_saved_row(db_session, today):
    upsert_daily_summary(
        db_session,
        production_date=today,
        shift="Day",
        drawers_inspected=10,
        drawers_rejected_unique=1,
        drawers_reworked=0,
        drawers_scrapped=None,
        notes=None,
    )
    row = get_daily_summary(db_session, today, "Day")
    assert row is not None
    assert row.drawers_inspected == 10


def test_suggested_counts_are_zero_with_no_defect_cases(db_session, today):
    suggestion = suggested_daily_counts(db_session, today)
    assert suggestion["suggested_drawers_rejected_unique"] == 0
    assert suggestion["defect_case_count"] == 0


def test_suggested_rejected_counts_distinct_cases_regardless_of_disposition(
    db_session, stations, categories, today
):
    # One case still open (no disposition yet)...
    create_defect_case(
        db_session,
        production_date=today,
        detected_at=_detected_at(today),
        work_order_number="WO-A",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[{"defect_category_id": categories["Sanding / Surface"].id}],
    )
    # ...another resolved on the spot as Rework/Use As Is outcome.
    create_defect_case(
        db_session,
        production_date=today,
        detected_at=_detected_at(today),
        work_order_number="WO-B",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[{"defect_category_id": categories["Sanding / Surface"].id}],
        disposition="Rework",
        resolved_on_the_spot=True,
        instant_close_outcome="Use As Is",
        repair_action="Buyer accepted as-is",
    )

    suggestion = suggested_daily_counts(db_session, today)
    assert suggestion["suggested_drawers_rejected_unique"] == 2
    assert suggestion["defect_case_count"] == 2


def test_suggested_rejected_dedups_two_categories_on_one_case_as_one_drawer(
    db_session, stations, categories, today
):
    """PROJECT_SPEC.md section 2: two categories on one drawer is still one
    defective drawer - the suggestion must not double-count it."""
    create_defect_case(
        db_session,
        production_date=today,
        detected_at=_detected_at(today),
        work_order_number="WO-TWO-CAT",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[
            {"defect_category_id": categories["Sanding / Surface"].id},
            {"defect_category_id": categories["Dado / Bottom Groove"].id},
        ],
    )

    suggestion = suggested_daily_counts(db_session, today)
    assert suggestion["suggested_drawers_rejected_unique"] == 1
    assert suggestion["defect_case_count"] == 1


def test_suggested_counts_ignore_soft_deleted_cases(db_session, stations, categories, today):
    from app.services.defect_service import soft_delete_case

    case = create_defect_case(
        db_session,
        production_date=today,
        detected_at=_detected_at(today),
        work_order_number="WO-DELETED",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[{"defect_category_id": categories["Sanding / Surface"].id}],
    )
    soft_delete_case(db_session, case)

    suggestion = suggested_daily_counts(db_session, today)
    assert suggestion["suggested_drawers_rejected_unique"] == 0


def test_suggested_counts_only_include_the_given_production_date(
    db_session, stations, categories, today
):
    other_day = today - dt.timedelta(days=1)
    create_defect_case(
        db_session,
        production_date=other_day,
        detected_at=_detected_at(other_day),
        work_order_number="WO-YESTERDAY",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[{"defect_category_id": categories["Sanding / Surface"].id}],
    )

    suggestion = suggested_daily_counts(db_session, today)
    assert suggestion["suggested_drawers_rejected_unique"] == 0


# ---------------------------------------------------------------------------
# count_rework_cases_by_date - the Daily Summary page's read-only "Reworked
# (from cases)" column (PROJECT_SPEC_PHASE7.md: drawers_reworked left the form
# entirely, but a read-only case-derived figure was added back for reference).
# ---------------------------------------------------------------------------


def test_count_rework_cases_by_date_counts_only_rework_disposition(
    db_session, stations, categories, today
):
    create_defect_case(
        db_session,
        production_date=today,
        detected_at=_detected_at(today),
        work_order_number="WO-CR-1",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[{"defect_category_id": categories["Sanding / Surface"].id}],
        disposition="Rework",
    )
    create_defect_case(
        db_session,
        production_date=today,
        detected_at=_detected_at(today),
        work_order_number="WO-CR-2",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[{"defect_category_id": categories["Sanding / Surface"].id}],
        disposition="Set Aside",
    )

    counts = count_rework_cases_by_date(db_session, [today])
    assert counts[today] == 1


def test_count_rework_cases_by_date_ignores_status(db_session, stations, categories, today):
    """No status qualifier - any Rework-dispositioned case counts, open or
    closed, matching Rework Rate's own rule exactly."""
    create_defect_case(
        db_session,
        production_date=today,
        detected_at=_detected_at(today),
        work_order_number="WO-CR-3",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[{"defect_category_id": categories["Sanding / Surface"].id}],
        disposition="Rework",
        resolved_on_the_spot=True,
        repair_action="Resanded",
    )

    counts = count_rework_cases_by_date(db_session, [today])
    assert counts[today] == 1


def test_count_rework_cases_by_date_omits_dates_with_no_matches(db_session, today):
    assert count_rework_cases_by_date(db_session, [today]) == {}


def test_count_rework_cases_by_date_empty_input_is_empty(db_session):
    assert count_rework_cases_by_date(db_session, []) == {}
