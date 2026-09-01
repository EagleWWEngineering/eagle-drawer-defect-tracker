"""PROJECT_SPEC_PHASE9.md Part 1/2: line_label normalisation and storage."""

from __future__ import annotations

import datetime as dt

from app.services import defect_service


def test_normalize_line_label_uppercases_and_strips_whitespace():
    assert defect_service.normalize_line_label("  e  ") == "E"
    assert defect_service.normalize_line_label("ab") == "AB"


def test_normalize_line_label_blank_becomes_none():
    assert defect_service.normalize_line_label("") is None
    assert defect_service.normalize_line_label("   ") is None
    assert defect_service.normalize_line_label(None) is None


def _create_case(db_session, stations, categories, today, **overrides):
    payload = {
        "production_date": today,
        "detected_at": dt.datetime(2026, 7, 24, 14, 30, tzinfo=dt.timezone.utc),
        "work_order_number": "178414",
        "drawer_part_reference": None,
        "found_station_id": stations["QC / Sorting / Shipping"].id,
        "possible_source_station_id": None,
        "priority": "Normal",
        "items": [
            {
                "defect_category_id": categories["Sanding / Surface"].id,
                "affected_drawer_quantity": 1,
            }
        ],
    }
    payload.update(overrides)
    return defect_service.create_defect_case(db_session, **payload)


def test_new_case_has_null_line_label_and_entry_source_by_default(
    db_session, stations, categories, today
):
    case = _create_case(db_session, stations, categories, today)
    assert case.line_label is None
    assert case.entry_source is None


def test_line_label_is_normalized_on_create(db_session, stations, categories, today):
    case = _create_case(db_session, stations, categories, today, line_label="  e  ")
    assert case.line_label == "E"


def test_two_cases_may_share_the_same_order_and_line(db_session, stations, categories, today):
    first = _create_case(db_session, stations, categories, today, line_label="E")
    second = _create_case(db_session, stations, categories, today, line_label="E")
    assert first.id != second.id
    assert first.line_label == second.line_label == "E"


def test_entry_source_is_stored_as_given(db_session, stations, categories, today):
    case = _create_case(
        db_session, stations, categories, today, line_label="B", entry_source="scanned"
    )
    assert case.entry_source == "scanned"
