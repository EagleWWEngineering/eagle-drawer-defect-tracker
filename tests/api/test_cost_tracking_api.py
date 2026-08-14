"""API tests for Phase 4 internal cost tracking: reports/summary + trend cost
fields, daily-production cost fields, cost settings, and the combined
internal+external total quality cost."""

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


def test_daily_production_upsert_and_list_include_cost_fields(client):
    resp = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 100,
            "drawers_rejected_unique": 10,
            "drawers_reworked": 5,
            "drawers_scrapped": 2,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cost_per_drawer_at_time"] == 35.0
    assert body["internal_rework_cost"] == 175.0
    assert body["internal_scrap_cost"] == 70.0

    list_resp = client.get("/api/v1/daily-production")
    row = list_resp.json()[0]
    assert row["internal_rework_cost"] == 175.0
    assert row["internal_scrap_cost"] == 70.0


def test_reports_summary_includes_internal_cost_fields(client, master_data):
    client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 100,
            "drawers_rejected_unique": 10,
            "drawers_reworked": 5,
            "drawers_scrapped": 2,
        },
    )
    resp = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    )
    body = resp.json()
    assert "internal_scrap_cost" not in body  # dropped entirely - see PHASE4 doc
    assert body["internal_rework_cost"] == 175.0
    assert body["total_internal_quality_cost"] == 175.0  # rework only, no scrap
    assert body["quality_cost_per_drawer_inspected"] == 1.75


def test_reports_summary_zero_inspected_returns_null_cost_per_drawer(client):
    resp = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    )
    body = resp.json()
    assert body["quality_cost_per_drawer_inspected"] is None
    assert body["total_internal_quality_cost"] == 0.0


def test_reports_trend_includes_per_bucket_cost(client):
    client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 100,
            "drawers_rejected_unique": 10,
            "drawers_reworked": 5,
            "drawers_scrapped": 2,
        },
    )
    client.put(
        "/api/v1/daily-production/2026-07-25",
        json={
            "shift": "Day",
            "drawers_inspected": 50,
            "drawers_rejected_unique": 5,
            "drawers_reworked": 2,
            "drawers_scrapped": 1,
        },
    )
    resp = client.get(
        "/api/v1/reports/trend", params={"start_date": "2026-07-24", "end_date": "2026-07-25"}
    )
    points = {p["period"]: p for p in resp.json()}
    assert points["2026-07-24"]["internal_rework_cost"] == 175.0
    assert points["2026-07-25"]["internal_rework_cost"] == 70.0  # 2 * 35


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


def test_rate_change_does_not_alter_already_saved_summary_via_api(client):
    client.put(
        "/api/v1/daily-production/2026-07-20",
        json={
            "shift": "Day",
            "drawers_inspected": 100,
            "drawers_rejected_unique": 10,
            "drawers_reworked": 5,
            "drawers_scrapped": 2,
        },
    )
    client.put("/api/v1/settings/cost-per-drawer", json={"cost_per_drawer": 60.0})

    rows = client.get("/api/v1/daily-production").json()
    old_row = next(r for r in rows if r["production_date"] == "2026-07-20")
    assert old_row["cost_per_drawer_at_time"] == 35.0
    assert old_row["internal_rework_cost"] == 175.0  # still 5 * 35, not 5 * 60

    new_resp = client.put(
        "/api/v1/daily-production/2026-07-21",
        json={
            "shift": "Day",
            "drawers_inspected": 80,
            "drawers_rejected_unique": 8,
            "drawers_reworked": 4,
            "drawers_scrapped": 1,
        },
    )
    assert new_resp.json()["cost_per_drawer_at_time"] == 60.0
    assert new_resp.json()["internal_rework_cost"] == 240.0  # 4 * 60


def test_combined_internal_and_external_total_quality_cost(
    client, master_data, customer_categories
):
    client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 100,
            "drawers_rejected_unique": 10,
            "drawers_reworked": 5,
            "drawers_scrapped": 2,
        },
    )
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
    # 175 internal (5*35 rework only - scrap dropped, see PHASE4 doc) + 200
    # external (2 pieces * $100)
    assert combined_total_quality_cost == 375.0


# ---------------------------------------------------------------------------
# Phase 4 fix: internal cost must never show $0 just because nobody filled out a
# Daily Production Summary, as long as real defect cases with a Rework/Scrap
# disposition exist for that period.
# ---------------------------------------------------------------------------


def test_summary_cost_falls_back_to_defect_cases_with_no_daily_summary(client, master_data):
    case = _create_case(client, master_data).json()
    client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "In Rework", "disposition": "Rework"},
    )

    resp = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    )
    body = resp.json()
    assert body["internal_rework_cost"] == 35.0  # 1 case * $35, not $0
    assert body["cost_basis"] == "defect_cases"
    assert body["defect_case_rework_count"] == 1


def test_summary_cost_blends_daily_summary_and_defect_cases_by_date(client, master_data):
    # 2026-07-24 has a daily summary; 2026-07-25 only has a defect case.
    client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 100,
            "drawers_rejected_unique": 10,
            "drawers_reworked": 5,
            "drawers_scrapped": 2,
        },
    )
    case = _create_case(
        client, master_data, production_date="2026-07-25", detected_at="2026-07-25T09:00:00Z"
    ).json()
    client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "In Rework", "disposition": "Rework"},
    )

    resp = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-25"}
    )
    body = resp.json()
    assert body["internal_rework_cost"] == 175.0 + 35.0  # 5*35 from summary + 1*35 from the case
    assert body["cost_basis"] == "blended"
    assert body["defect_case_rework_count"] == 1


def test_summary_cost_prefers_daily_summary_for_dates_that_have_one(client, master_data):
    """A case on a date that DOES have a Daily Production Summary must not also be
    counted through the defect-case fallback (no double counting)."""
    client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 100,
            "drawers_rejected_unique": 10,
            "drawers_reworked": 5,
            "drawers_scrapped": 2,
        },
    )
    case = _create_case(client, master_data).json()
    client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "In Rework", "disposition": "Rework"},
    )

    resp = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    )
    body = resp.json()
    assert body["internal_rework_cost"] == 175.0  # 5*35, the case is NOT added on top
    assert body["cost_basis"] == "daily_summary"
    assert body["defect_case_rework_count"] == 0


def test_summary_cost_basis_is_none_when_nothing_recorded(client):
    resp = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    )
    body = resp.json()
    assert body["cost_basis"] == "none"
    assert body["internal_rework_cost"] == 0.0


def test_trend_cost_also_falls_back_to_defect_cases(client, master_data):
    case = _create_case(
        client, master_data, production_date="2026-07-26", detected_at="2026-07-26T09:00:00Z"
    ).json()
    client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "In Rework", "disposition": "Rework"},
    )

    resp = client.get(
        "/api/v1/reports/trend", params={"start_date": "2026-07-26", "end_date": "2026-07-26"}
    )
    points = {p["period"]: p for p in resp.json()}
    assert points["2026-07-26"]["internal_rework_cost"] == 35.0
