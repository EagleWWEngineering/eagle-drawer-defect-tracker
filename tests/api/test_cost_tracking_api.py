"""API tests for the PROJECT_SPEC_PHASE7.md "Cost model": one cost unit per
defect case, snapshotted at creation, zero for a case closed "Closed - Use As
Is" (which feeds Cost Avoided instead). Replaces the Phase 4 dual-source
(Daily Production Summary + defect-case fallback) model entirely.
"""

from __future__ import annotations


def _create_case(client, master_data, **overrides):
    payload = {
        "production_date": "2026-07-24",
        "detected_at": "2026-07-24T14:30:00Z",
        "work_order_number": "WO-COST-1",
        "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
        "priority": "Normal",
        "items": [
            {
                "defect_category_id": master_data["categories"]["Sanding / Surface"],
                "affected_drawer_quantity": 1,
            }
        ],
    }
    payload.update(overrides)
    return client.post("/api/v1/defect-cases", json=payload)


def test_created_case_snapshots_the_current_rate(client, master_data):
    case = _create_case(client, master_data).json()
    assert case["cost_per_drawer_at_time"] == 35.0  # seeded default


def test_daily_production_summary_no_longer_reports_cost_fields(client):
    """PROJECT_SPEC_PHASE7.md: DailyProductionSummaryOut's internal_rework_cost/
    internal_scrap_cost were removed - cost is entirely case-derived now, not
    computed from this row's drawers_reworked/drawers_scrapped * rate anymore."""
    resp = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={"shift": "Day", "drawers_inspected": 100, "drawers_rejected_unique": 10},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "internal_rework_cost" not in body
    assert "internal_scrap_cost" not in body
    assert body["cost_per_drawer_at_time"] == 35.0  # still snapshotted, just unused for cost


def test_reports_summary_one_unit_per_case_not_per_item_or_quantity(client, master_data):
    """A case with two categories and affected_drawer_quantity > 1 still
    contributes exactly ONE cost unit - never multiplied."""
    _create_case(
        client,
        master_data,
        items=[
            {
                "defect_category_id": master_data["categories"]["Sanding / Surface"],
                "affected_drawer_quantity": 5,
            },
            {
                "defect_category_id": master_data["categories"]["Dado / Bottom Groove"],
                "affected_drawer_quantity": 3,
            },
        ],
    )
    resp = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    )
    body = resp.json()
    assert body["internal_rework_cost"] == 35.0  # one case, one unit - not 5, not 8, not 35*8
    assert body["total_internal_quality_cost"] == 35.0


def test_reports_summary_use_as_is_contributes_zero_and_feeds_cost_avoided(client, master_data):
    case = _create_case(
        client,
        master_data,
        disposition="Rework",
        resolved_on_the_spot=True,
        instant_close_outcome="Use As Is",
        repair_action="Buyer accepted as-is",
    ).json()
    assert case["status"] == "Closed - Use As Is"

    resp = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    )
    body = resp.json()
    assert body["internal_rework_cost"] == 0.0
    assert body["cost_avoided"] == 35.0
    assert body["total_internal_quality_cost"] == 0.0


def test_reports_summary_scrapped_case_counts_normally_not_zeroed(client, master_data):
    """Closed - Scrapped counts as one unit like any other non-Use-As-Is case -
    only Use As Is is special-cased. (Scrap itself is retired for new entry, so
    this simulates a historical scrapped case still contributing cost.)"""
    case = _create_case(client, master_data).json()
    from app.models import DefectCase

    session = client.testing_sessionmaker()
    db_case = session.get(DefectCase, case["id"])
    db_case.status = "Closed - Scrapped"
    session.commit()
    session.close()

    resp = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    )
    body = resp.json()
    assert body["internal_rework_cost"] == 35.0
    assert body["cost_avoided"] == 0.0


def test_reports_summary_open_cases_count_immediately(client, master_data):
    """Open cases count their cost right away - cost is removed only if/when the
    case closes Closed - Use As Is."""
    _create_case(client, master_data)  # left Open, no disposition

    resp = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    )
    body = resp.json()
    assert body["internal_rework_cost"] == 35.0


def test_reports_summary_zero_cases_is_zero_cost(client):
    resp = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    )
    body = resp.json()
    assert body["internal_rework_cost"] == 0.0
    assert body["cost_avoided"] == 0.0
    assert body["total_internal_quality_cost"] == 0.0
    assert body["quality_cost_per_drawer_inspected"] is None


def test_rate_change_does_not_alter_already_created_case(client, master_data):
    """PROJECT_SPEC_PHASE7.md: the rate is snapshotted at case CREATION time and
    never re-priced later, even if the Admin rate changes - same discipline as
    DailyProductionSummary.cost_per_drawer_at_time before it."""
    old_case = _create_case(client, master_data, work_order_number="WO-COST-OLD").json()
    assert old_case["cost_per_drawer_at_time"] == 35.0

    client.put("/api/v1/settings/cost-per-drawer", json={"cost_per_drawer": 60.0})

    new_case = _create_case(client, master_data, work_order_number="WO-COST-NEW").json()
    assert new_case["cost_per_drawer_at_time"] == 60.0

    fetched_old = client.get(f"/api/v1/defect-cases/{old_case['id']}").json()
    assert (
        fetched_old["cost_per_drawer_at_time"] == 35.0
    ), "the old case must keep its original rate after the rate changes"


def test_null_snapshot_falls_back_to_current_rate(client, master_data):
    """A case created before the cost_per_drawer_at_time column existed (null
    snapshot) must fall back to the currently-configured rate, never $0."""
    case = _create_case(client, master_data).json()
    from app.models import DefectCase

    session = client.testing_sessionmaker()
    db_case = session.get(DefectCase, case["id"])
    db_case.cost_per_drawer_at_time = None
    session.commit()
    session.close()

    resp = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    )
    body = resp.json()
    assert body["internal_rework_cost"] == 35.0  # fell back to the current rate, not $0


def test_reports_trend_includes_cost_avoided_per_bucket(client, master_data):
    _create_case(client, master_data, work_order_number="WO-TREND-1")  # $35 rework cost
    _create_case(
        client,
        master_data,
        work_order_number="WO-TREND-2",
        disposition="Rework",
        resolved_on_the_spot=True,
        instant_close_outcome="Use As Is",
        repair_action="Buyer accepted as-is",
    )  # $35 avoided

    resp = client.get(
        "/api/v1/reports/trend", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    )
    points = {p["period"]: p for p in resp.json()}
    point = points["2026-07-24"]
    assert point["internal_rework_cost"] == 35.0
    assert point["cost_avoided"] == 35.0


def test_reports_summary_rework_rate_counts_rework_disposition_cases(client, master_data):
    """PROJECT_SPEC_PHASE7.md: Rework Rate = cases with disposition Rework /
    drawers_inspected * 100 - no status qualifier, and no more reading
    DailyProductionSummary.drawers_reworked."""
    client.put(
        "/api/v1/daily-production/2026-07-24",
        json={"shift": "Day", "drawers_inspected": 100, "drawers_rejected_unique": 10},
    )
    _create_case(client, master_data, work_order_number="WO-RR-1", disposition="Rework")
    _create_case(
        client,
        master_data,
        work_order_number="WO-RR-2",
        disposition="Rework",
        resolved_on_the_spot=True,
        repair_action="Resanded",
    )
    _create_case(client, master_data, work_order_number="WO-RR-3", disposition="Set Aside")

    resp = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    )
    body = resp.json()
    assert body["drawers_reworked"] == 2  # 2 Rework-dispositioned cases, not the Set Aside one
    assert body["rework_rate"] == 2.0  # 2 / 100 * 100


def test_get_and_update_cost_per_drawer_setting(client):
    resp = client.get("/api/v1/settings/cost-per-drawer")
    assert resp.status_code == 200
    assert resp.json()["cost_per_drawer"] == 35.0

    update_resp = client.put("/api/v1/settings/cost-per-drawer", json={"cost_per_drawer": 42.5})
    assert update_resp.status_code == 200
    assert update_resp.json()["cost_per_drawer"] == 42.5

    confirm_resp = client.get("/api/v1/settings/cost-per-drawer")
    assert confirm_resp.json()["cost_per_drawer"] == 42.5


def test_update_cost_per_drawer_rejects_non_positive(client):
    resp = client.put("/api/v1/settings/cost-per-drawer", json={"cost_per_drawer": 0})
    assert resp.status_code == 422  # Pydantic Field(gt=0) validation


def test_combined_internal_and_external_total_quality_cost(
    client, master_data, customer_categories
):
    _create_case(client, master_data)  # $35 internal
    client.post(
        "/api/v1/customer-issues",
        json={
            "reported_date": "2026-07-24",
            "customer_name": "Jordan Ellis",
            "issue_category_id": customer_categories["Wrong Size"],
            "source_type": "Manufacturing",
            "piece_count": 2,
            "description": "Wrong size reported.",
        },
    )

    internal = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    ).json()
    external = client.get(
        "/api/v1/customer-issues/summary",
        params={"start_date": "2026-07-24", "end_date": "2026-07-24"},
    ).json()

    combined_total_quality_cost = (
        internal["total_internal_quality_cost"] + external["total_estimated_cost"]
    )
    # 35 internal (one case) + 200 external (2 pieces * $100)
    assert combined_total_quality_cost == 235.0
