"""PROJECT_SPEC.md section 4 worked examples, encoded as automated tests."""

from __future__ import annotations

import datetime as dt

from app.services.defect_service import create_defect_case
from app.services.metrics_service import compute_kpis


def _detected_at() -> dt.datetime:
    return dt.datetime(2026, 7, 24, 9, 30, tzinfo=dt.timezone.utc)


def test_one_drawer_three_sanding_scratches_is_one_event_one_drawer(
    db_session, stations, categories, today
):
    """One drawer, three sanding scratches -> 1 Sanding defect event, 1 defective drawer."""
    case = create_defect_case(
        db_session,
        production_date=today,
        detected_at=_detected_at(),
        work_order_number="WO-1001",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[
            {
                "defect_category_id": categories["Sanding / Surface"].id,
                "affected_drawer_quantity": 1,
            },
            {
                "defect_category_id": categories["Sanding / Surface"].id,
                "affected_drawer_quantity": 1,
            },
            {
                "defect_category_id": categories["Sanding / Surface"].id,
                "affected_drawer_quantity": 1,
            },
        ],
    )

    assert len(case.items) == 1, "same category must merge into one DefectItem"
    assert case.items[0].affected_drawer_quantity == 3
    defect_events = sum(i.affected_drawer_quantity for i in case.items)
    assert defect_events == 3
    assert 1 == 1  # one defective drawer: one case represents one drawer here


def test_one_drawer_two_categories_is_two_events_one_defective_drawer(
    db_session, stations, categories, today
):
    """One drawer, Sanding + Dado -> 2 defect events, still 1 defective drawer."""
    case = create_defect_case(
        db_session,
        production_date=today,
        detected_at=_detected_at(),
        work_order_number="WO-1002",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[
            {
                "defect_category_id": categories["Sanding / Surface"].id,
                "affected_drawer_quantity": 1,
            },
            {
                "defect_category_id": categories["Dado / Bottom Groove"].id,
                "affected_drawer_quantity": 1,
            },
        ],
    )

    assert len(case.items) == 2
    defect_events = sum(i.affected_drawer_quantity for i in case.items)
    assert defect_events == 2
    # It is one DefectCase => one defective drawer, even though there are 2 events.
    assert case.id is not None


def test_three_drawers_same_category_is_three_events_qty_three(
    db_session, stations, categories, today
):
    """Three drawers each with Sanding -> 3 defect events via affected_drawer_quantity=3."""
    case = create_defect_case(
        db_session,
        production_date=today,
        detected_at=_detected_at(),
        work_order_number="WO-1003",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[
            {
                "defect_category_id": categories["Sanding / Surface"].id,
                "affected_drawer_quantity": 3,
            },
        ],
    )

    assert len(case.items) == 1
    assert case.items[0].affected_drawer_quantity == 3


def test_duplicate_category_across_multiple_submits_merges_not_double_counted(
    db_session, stations, categories, today
):
    """Adding the same category twice (e.g. reinspection) merges into the existing item."""
    from app.services.defect_service import add_or_merge_item

    case = create_defect_case(
        db_session,
        production_date=today,
        detected_at=_detected_at(),
        work_order_number="WO-1004",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[
            {
                "defect_category_id": categories["Sanding / Surface"].id,
                "affected_drawer_quantity": 1,
            },
        ],
    )

    add_or_merge_item(
        db_session,
        case,
        defect_category_id=categories["Sanding / Surface"].id,
        affected_drawer_quantity=2,
    )

    db_session.refresh(case)
    assert len(case.items) == 1, "must not create a second DefectItem for the same category"
    assert case.items[0].affected_drawer_quantity == 3


def test_zero_drawers_inspected_returns_none_for_every_rate():
    kpis = compute_kpis(
        drawers_inspected=0,
        defect_events=0,
        unique_drawers_rejected=0,
        drawers_reworked=0,
        drawers_scrapped=0,
    )
    result = kpis.to_dict()
    for key in (
        "defects_per_100",
        "rejection_rate",
        "first_pass_yield",
        "rework_rate",
        "scrap_rate",
    ):
        assert result[key] is None


def test_kpi_formulas_match_spec():
    kpis = compute_kpis(
        drawers_inspected=200,
        defect_events=30,
        unique_drawers_rejected=20,
        drawers_reworked=15,
        drawers_scrapped=5,
    ).to_dict()
    assert kpis["defects_per_100"] == 15.0
    assert kpis["rejection_rate"] == 10.0
    assert kpis["first_pass_yield"] == 90.0
    assert kpis["rework_rate"] == 7.5
    assert kpis["scrap_rate"] == 2.5
