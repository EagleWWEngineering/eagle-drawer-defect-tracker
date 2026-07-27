"""Unit tests for app/services/customer_issue_service.py (Phase 2)."""

from __future__ import annotations

import datetime as dt
import decimal

import pytest

from app.errors import NotFoundError, ValidationError
from app.services import customer_issue_service as svc
from app.services.defect_service import create_defect_case
from app.services.metrics_service import compute_pareto


def _make_issue(db_session, customer_categories, today, **overrides):
    payload = dict(
        reported_date=today,
        customer_name="Jordan Ellis",
        order_number="SO-8842",
        issue_category_id=customer_categories["Wrong Size"].id,
        source_type="Manufacturing",
        should_have_caught_at="QA/Final",
        piece_count=2,
        estimated_rework_cost=None,
        description="Drawer box was 1/2 inch too short.",
        photo_urls=None,
        notes=None,
    )
    payload.update(overrides)
    return svc.create_customer_issue(db_session, **payload)


def test_issue_number_format_and_daily_sequence_reset(db_session, customer_categories, today):
    issue1 = _make_issue(db_session, customer_categories, today)
    issue2 = _make_issue(db_session, customer_categories, today)
    tomorrow = today + dt.timedelta(days=1)
    issue3 = _make_issue(db_session, customer_categories, tomorrow)

    assert issue1.issue_number == "CI-20260724-0001"
    assert issue2.issue_number == "CI-20260724-0002"
    assert issue3.issue_number == "CI-20260725-0001"  # sequence resets on a new date


def test_estimated_rework_cost_auto_calculated_when_not_provided(
    db_session, customer_categories, today
):
    issue = _make_issue(db_session, customer_categories, today, piece_count=3)
    assert issue.estimated_rework_cost == decimal.Decimal("300.00")


def test_estimated_rework_cost_respects_explicit_value(db_session, customer_categories, today):
    issue = _make_issue(
        db_session,
        customer_categories,
        today,
        piece_count=3,
        estimated_rework_cost=decimal.Decimal("250.00"),
    )
    assert issue.estimated_rework_cost == decimal.Decimal("250.00")


def test_missing_customer_name_is_rejected(db_session, customer_categories, today):
    with pytest.raises(ValidationError):
        _make_issue(db_session, customer_categories, today, customer_name="  ")


def test_invalid_source_type_is_rejected(db_session, customer_categories, today):
    with pytest.raises(ValidationError):
        _make_issue(db_session, customer_categories, today, source_type="Warehouse")


def test_order_number_optional_for_unidentified_orders(db_session, customer_categories, today):
    issue = _make_issue(db_session, customer_categories, today, order_number=None)
    assert issue.order_number is None


def test_update_resolves_order_number_and_notes(db_session, customer_categories, today):
    issue = _make_issue(db_session, customer_categories, today, order_number=None)
    updated = svc.update_customer_issue(
        db_session, issue, order_number="SO-9001", notes="Found via packing slip."
    )
    assert updated.order_number == "SO-9001"
    assert updated.notes == "Found via packing slip."


def test_ignore_sets_status(db_session, customer_categories, today):
    issue = _make_issue(db_session, customer_categories, today)
    ignored = svc.ignore_issue(db_session, issue)
    assert ignored.status == "Ignored"


def test_soft_delete_marks_deleted_and_get_or_404_hides_it(db_session, customer_categories, today):
    issue = _make_issue(db_session, customer_categories, today)
    svc.soft_delete_issue(db_session, issue)
    with pytest.raises(NotFoundError):
        svc.get_issue_or_404(db_session, issue.id)


def test_link_to_defect_case_sets_status_linked_and_case_id(
    db_session, stations, categories, customer_categories, today
):
    case = create_defect_case(
        db_session,
        production_date=today,
        detected_at=dt.datetime(2026, 7, 24, 10, 0, tzinfo=dt.timezone.utc),
        work_order_number="WO-LINK-1",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[
            {
                "defect_category_id": categories["Sanding / Surface"].id,
                "affected_drawer_quantity": 1,
            }
        ],
    )
    issue = _make_issue(db_session, customer_categories, today)

    linked = svc.link_to_defect_case(db_session, issue, case.id)
    assert linked.status == "Linked"
    assert linked.linked_defect_case_id == case.id


def test_link_to_nonexistent_case_raises_not_found(db_session, customer_categories, today):
    issue = _make_issue(db_session, customer_categories, today)
    with pytest.raises(NotFoundError):
        svc.link_to_defect_case(db_session, issue, 999999)


def test_compute_summary_totals(db_session, customer_categories, today):
    _make_issue(db_session, customer_categories, today, piece_count=2)
    _make_issue(
        db_session,
        customer_categories,
        today,
        piece_count=1,
        estimated_rework_cost=decimal.Decimal("50.00"),
    )
    issues = [
        svc.get_issue_or_404(db_session, 1),
        svc.get_issue_or_404(db_session, 2),
    ]
    summary = svc.compute_summary(issues)
    assert summary["total_issues"] == 2
    assert summary["total_pieces_affected"] == 3
    assert summary["total_estimated_cost"] == 250.0  # 200 (auto) + 50 (explicit)


def test_escape_rate_and_catch_rate_zero_denominator_returns_none():
    result = svc.compute_escape_and_catch_rates(
        customer_issue_count=0, drawers_inspected=0, internal_defect_events=0
    )
    assert result["escape_rate"] is None
    assert result["internal_catch_rate"] is None


def test_escape_rate_and_catch_rate_formulas():
    result = svc.compute_escape_and_catch_rates(
        customer_issue_count=5, drawers_inspected=100, internal_defect_events=45
    )
    assert result["escape_rate"] == 5.0
    assert result["internal_catch_rate"] == 90.0  # 45 / (45 + 5) * 100


def test_customer_issue_pareto_sorted_desc_with_cumulative_pct():
    counts = {"Wrong Size": 5, "Finish Quality": 3, "Joinery": 2}
    rows = compute_pareto(counts)
    assert [r["label"] for r in rows] == ["Wrong Size", "Finish Quality", "Joinery"]
    assert rows[0]["cumulative_pct"] == 50.0
    assert rows[-1]["cumulative_pct"] == 100.0


def test_unknown_category_raises_not_found(db_session, today):
    with pytest.raises(NotFoundError):
        svc.create_customer_issue(
            db_session,
            reported_date=today,
            customer_name="Alex Rivera",
            order_number=None,
            issue_category_id=999999,
            source_type="Manufacturing",
            should_have_caught_at=None,
            piece_count=1,
            estimated_rework_cost=None,
            description="Some issue",
            photo_urls=None,
            notes=None,
        )
