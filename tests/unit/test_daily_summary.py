"""PROJECT_SPEC.md section 2.3: daily production summary hard rule + soft warnings."""

from __future__ import annotations

import datetime as dt
import decimal

import pytest

from app.errors import ValidationError
from app.services import settings_service
from app.services.defect_service import upsert_daily_summary


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
