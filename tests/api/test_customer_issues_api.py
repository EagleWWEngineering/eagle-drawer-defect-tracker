"""API tests for /api/v1/customer-issues and its CSV export (Phase 2)."""

from __future__ import annotations

import csv
import io


def _create_issue(client, customer_categories, **overrides):
    payload = {
        "reported_date": "2026-07-24",
        "customer_name": "Jordan Ellis",
        "order_number": "SO-8842",
        "issue_category_id": customer_categories["Wrong Size"],
        "source_type": "Manufacturing",
        "should_have_caught_at": "QA/Final",
        "piece_count": 2,
        "description": "Drawer box was 1/2 inch too short.",
    }
    payload.update(overrides)
    return client.post("/api/v1/customer-issues", json=payload)


def test_create_and_retrieve_customer_issue(client, customer_categories):
    resp = _create_issue(client, customer_categories)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["issue_number"].startswith("CI-20260724-")
    assert body["status"] == "Open"
    assert body["estimated_rework_cost"] == 200.0  # 2 pieces * $100
    assert body["issue_category_name"] == "Wrong Size"

    get_resp = client.get(f"/api/v1/customer-issues/{body['id']}")
    assert get_resp.status_code == 200
    assert get_resp.json()["issue_number"] == body["issue_number"]


def test_missing_order_number_flagged_for_ui(client, customer_categories):
    resp = _create_issue(client, customer_categories, order_number=None)
    body = resp.json()
    assert body["order_number"] is None
    assert body["order_number_missing"] is True


def test_list_filters_by_customer_category_source_type_and_status(client, customer_categories):
    _create_issue(client, customer_categories, customer_name="Jordan Ellis", order_number="SO-1")
    _create_issue(
        client,
        customer_categories,
        customer_name="Morgan Lee",
        order_number="SO-2",
        issue_category_id=customer_categories["Finish Quality"],
        source_type="Shipping Damage",
    )

    def count(**params):
        return client.get("/api/v1/customer-issues", params=params).json()["total"]

    assert count(customer_name="Jordan") == 1
    assert count(category_id=customer_categories["Finish Quality"]) == 1
    assert count(source_type="Shipping Damage") == 1
    assert count(order_number="SO-2") == 1
    assert count() == 2


def test_update_resolves_order_number_and_notes(client, customer_categories):
    created = _create_issue(client, customer_categories, order_number=None).json()
    resp = client.patch(
        f"/api/v1/customer-issues/{created['id']}",
        json={"order_number": "SO-9500", "notes": "Confirmed via email reply."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["order_number"] == "SO-9500"
    assert body["notes"] == "Confirmed via email reply."
    assert body["order_number_missing"] is False


def test_ignore_via_status_update(client, customer_categories):
    created = _create_issue(client, customer_categories).json()
    resp = client.patch(f"/api/v1/customer-issues/{created['id']}", json={"status": "Ignored"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "Ignored"


def test_link_to_defect_case_via_update(client, master_data, customer_categories):
    case_payload = {
        "production_date": "2026-07-24",
        "detected_at": "2026-07-24T14:30:00Z",
        "work_order_number": "WO-CI-LINK",
        "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
        "priority": "Normal",
        "items": [
            {
                "defect_category_id": master_data["categories"]["Sanding / Surface"],
                "affected_drawer_quantity": 1,
            }
        ],
    }
    case = client.post("/api/v1/defect-cases", json=case_payload).json()
    issue = _create_issue(client, customer_categories).json()

    resp = client.patch(
        f"/api/v1/customer-issues/{issue['id']}", json={"link_defect_case_id": case["id"]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Linked"
    assert body["linked_defect_case_id"] == case["id"]
    assert body["linked_defect_case_number"] == case["case_number"]


def test_soft_delete_hides_from_normal_queries_but_preserves_record(client, customer_categories):
    created = _create_issue(client, customer_categories, order_number="SO-DEL").json()

    del_resp = client.delete(f"/api/v1/customer-issues/{created['id']}")
    assert del_resp.status_code == 200

    list_resp = client.get("/api/v1/customer-issues", params={"order_number": "SO-DEL"})
    assert list_resp.json()["total"] == 0

    get_resp = client.get(f"/api/v1/customer-issues/{created['id']}")
    assert get_resp.status_code == 404

    include_resp = client.get(
        "/api/v1/customer-issues", params={"order_number": "SO-DEL", "include_deleted": True}
    )
    assert include_resp.json()["total"] == 1


def test_pareto_by_category_sorted_desc_with_cumulative_pct(client, customer_categories):
    _create_issue(client, customer_categories, issue_category_id=customer_categories["Wrong Size"])
    _create_issue(client, customer_categories, issue_category_id=customer_categories["Wrong Size"])
    _create_issue(
        client, customer_categories, issue_category_id=customer_categories["Finish Quality"]
    )

    rows = client.get(
        "/api/v1/customer-issues/pareto",
        params={"start_date": "2026-07-24", "end_date": "2026-07-24"},
    ).json()
    assert [r["label"] for r in rows] == ["Wrong Size", "Finish Quality"]
    assert rows[0]["issue_count"] == 2
    assert rows[0]["cumulative_pct"] == round(2 / 3 * 100, 1)
    assert rows[-1]["cumulative_pct"] == 100.0


def test_pareto_by_should_have_caught_at(client, customer_categories):
    _create_issue(client, customer_categories, should_have_caught_at="QA/Final")
    _create_issue(client, customer_categories, should_have_caught_at="Assembly")
    _create_issue(client, customer_categories, should_have_caught_at="Assembly")

    rows = client.get(
        "/api/v1/customer-issues/pareto",
        params={
            "start_date": "2026-07-24",
            "end_date": "2026-07-24",
            "group_by": "should_have_caught_at",
        },
    ).json()
    assert rows[0]["label"] == "Assembly"
    assert rows[0]["issue_count"] == 2


def test_summary_totals_and_zero_denominator_rates(client, customer_categories):
    _create_issue(client, customer_categories, piece_count=2)
    _create_issue(client, customer_categories, piece_count=1)

    resp = client.get(
        "/api/v1/customer-issues/summary",
        params={"start_date": "2026-07-24", "end_date": "2026-07-24"},
    )
    body = resp.json()
    assert body["total_issues"] == 2
    assert body["total_pieces_affected"] == 3
    assert body["total_estimated_cost"] == 300.0
    # No DailyProductionSummary rows exist for this date -> drawers_inspected == 0.
    assert body["escape_rate"] is None
    # Internal Catch Rate is null only when BOTH internal events and customer issues
    # are zero (PROJECT_SPEC_PHASE2.md). Here customer_issue_count=2 makes the
    # denominator nonzero, so a 0.0% catch rate is the mathematically correct value.
    assert body["internal_catch_rate"] == 0.0


def test_escape_rate_and_catch_rate_with_real_denominators(
    client, master_data, customer_categories
):
    client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 100,
            "drawers_rejected_unique": 5,
            "drawers_reworked": 3,
            "drawers_scrapped": 1,
        },
    )
    case_payload = {
        "production_date": "2026-07-24",
        "detected_at": "2026-07-24T14:30:00Z",
        "work_order_number": "WO-CATCH",
        "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
        "priority": "Normal",
        "items": [
            {
                "defect_category_id": master_data["categories"]["Sanding / Surface"],
                "affected_drawer_quantity": 45,
            }
        ],
    }
    client.post("/api/v1/defect-cases", json=case_payload)
    _create_issue(client, customer_categories)  # 1 customer issue on this date

    resp = client.get(
        "/api/v1/customer-issues/summary",
        params={"start_date": "2026-07-24", "end_date": "2026-07-24"},
    )
    body = resp.json()
    assert body["escape_rate"] == 1.0  # 1 / 100 * 100
    assert body["internal_catch_rate"] == 97.8  # 45 / (45 + 1) * 100, rounded


def test_csv_export_matches_filters_and_flags_missing_order(client, customer_categories):
    _create_issue(client, customer_categories, customer_name="Jordan Ellis", order_number=None)
    _create_issue(client, customer_categories, customer_name="Morgan Lee", order_number="SO-3")

    resp = client.get("/api/v1/exports/customer-issues.csv", params={"customer_name": "Jordan"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")

    rows = list(csv.reader(io.StringIO(resp.text)))
    header, data_rows = rows[0], rows[1:]
    assert len(data_rows) == 1
    assert data_rows[0][header.index("order_number")] == "ORDER NOT IDENTIFIED"


def test_export_is_audited(client, customer_categories):
    _create_issue(client, customer_categories)
    client.get("/api/v1/exports/customer-issues.csv")

    from app.models import AuditLog

    session = client.testing_sessionmaker()
    entries = (
        session.query(AuditLog)
        .filter(AuditLog.action == "export", AuditLog.entity_type == "CustomerIssue")
        .all()
    )
    assert len(entries) == 1
    session.close()


def test_create_is_audited(client, customer_categories):
    created = _create_issue(client, customer_categories).json()

    from app.models import AuditLog

    session = client.testing_sessionmaker()
    entries = (
        session.query(AuditLog)
        .filter(AuditLog.action == "create", AuditLog.entity_type == "CustomerIssue")
        .all()
    )
    assert len(entries) == 1
    assert entries[0].entity_id == created["issue_number"]
    session.close()


def test_invalid_source_type_returns_400(client, customer_categories):
    resp = _create_issue(client, customer_categories, source_type="Warehouse")
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "source_type"


def test_categories_route_lists_all_ten_seeded_categories(client):
    resp = client.get("/api/v1/customer-issues/categories")
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()]
    assert names[0] == "Wrong Size"
    assert len(names) == 10
