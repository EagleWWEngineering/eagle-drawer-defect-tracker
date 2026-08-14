"""API tests for /reports/* and /rework-queue."""

from __future__ import annotations


def _create_case(
    client,
    master_data,
    category_name,
    wo="WO-9001",
    priority="Normal",
    qty=1,
    production_date="2026-07-24",
):
    payload = {
        "production_date": production_date,
        "detected_at": f"{production_date}T14:30:00Z",
        "work_order_number": wo,
        "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
        "priority": priority,
        "items": [
            {
                "defect_category_id": master_data["categories"][category_name],
                "affected_drawer_quantity": qty,
            }
        ],
    }
    return client.post("/api/v1/defect-cases", json=payload).json()


def test_summary_kpis_match_spec_formulas(client, master_data):
    client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 100,
            "drawers_rejected_unique": 10,
            "drawers_reworked": 7,
            "drawers_scrapped": 2,
        },
    )
    _create_case(client, master_data, "Sanding / Surface", wo="WO-A", qty=2)
    _create_case(client, master_data, "Dado / Bottom Groove", wo="WO-B", qty=1)

    resp = client.get(
        "/api/v1/reports/summary",
        params={"start_date": "2026-07-24", "end_date": "2026-07-24"},
    )
    body = resp.json()
    assert body["defect_events"] == 3
    assert body["drawers_inspected"] == 100
    assert body["defects_per_100"] == 3.0
    assert body["rejection_rate"] == 10.0
    assert body["first_pass_yield"] == 90.0


def test_summary_zero_inspected_returns_null_rates(client, master_data):
    _create_case(client, master_data, "Sanding / Surface")
    resp = client.get(
        "/api/v1/reports/summary",
        params={"start_date": "2026-07-24", "end_date": "2026-07-24"},
    )
    body = resp.json()
    assert body["defects_per_100"] is None
    assert body["rejection_rate"] is None


def test_pareto_sorted_desc_with_cumulative_and_total_matches_filtered(client, master_data):
    _create_case(client, master_data, "Sanding / Surface", wo="WO-P1", qty=5)
    _create_case(client, master_data, "Dado / Bottom Groove", wo="WO-P2", qty=3)
    _create_case(client, master_data, "Other", wo="WO-P3", qty=2)

    resp = client.get(
        "/api/v1/reports/pareto",
        params={"start_date": "2026-07-24", "end_date": "2026-07-24"},
    )
    rows = resp.json()
    assert [r["label"] for r in rows] == ["Sanding / Surface", "Dado / Bottom Groove", "Other"]
    assert rows[0]["cumulative_pct"] == 50.0
    assert rows[-1]["cumulative_pct"] == 100.0

    list_resp = client.get(
        "/api/v1/defect-cases",
        params={"start_date": "2026-07-24", "end_date": "2026-07-24"},
    )
    assert list_resp.json()["total"] == 3
    assert sum(r["defect_events"] for r in rows) == 10


def test_pareto_by_source_station_never_calls_it_root_cause(client, master_data):
    payload = {
        "production_date": "2026-07-24",
        "detected_at": "2026-07-24T14:30:00Z",
        "work_order_number": "WO-SRC",
        "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
        "possible_source_station_id": master_data["stations"]["Dado"],
        "priority": "Normal",
        "items": [
            {
                "defect_category_id": master_data["categories"]["Dado / Bottom Groove"],
                "affected_drawer_quantity": 1,
            }
        ],
    }
    client.post("/api/v1/defect-cases", json=payload)
    resp = client.get(
        "/api/v1/reports/pareto",
        params={
            "start_date": "2026-07-24",
            "end_date": "2026-07-24",
            "group_by": "source_station",
        },
    )
    rows = resp.json()
    assert any(r["label"] == "Dado" for r in rows)


def test_work_order_history_returns_only_that_work_order(client, master_data):
    _create_case(client, master_data, "Sanding / Surface", wo="WO-HIST-1")
    _create_case(client, master_data, "Dado / Bottom Groove", wo="WO-HIST-1")
    _create_case(client, master_data, "Other", wo="WO-HIST-2")

    resp = client.get("/api/v1/reports/work-orders/WO-HIST-1")
    body = resp.json()
    assert len(body["cases"]) == 2
    assert body["total_defect_events"] == 2


def test_rework_queue_sorts_urgent_first_then_oldest(client, master_data):
    _create_case(client, master_data, "Sanding / Surface", wo="WO-Q-NORMAL", priority="Normal")
    _create_case(client, master_data, "Sanding / Surface", wo="WO-Q-URGENT", priority="Urgent")
    _create_case(client, master_data, "Sanding / Surface", wo="WO-Q-HIGH", priority="High")

    resp = client.get("/api/v1/rework-queue")
    rows = resp.json()
    priorities = [r["priority"] for r in rows]
    assert priorities.index("Urgent") < priorities.index("High") < priorities.index("Normal")


def test_rework_queue_excludes_closed_cases(client, master_data):
    case = _create_case(client, master_data, "Sanding / Surface", wo="WO-CLOSED")
    close_resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "Closed - Use As Is", "note": "Decided to use as is."},
    )
    assert close_resp.status_code == 200, close_resp.text
    resp = client.get("/api/v1/rework-queue")
    work_orders = [r["work_order_number"] for r in resp.json()]
    assert "WO-CLOSED" not in work_orders


def test_rework_queue_includes_root_cause_fields_for_later_editing(client, master_data):
    """Root cause / corrective action / repair action are filled in from the Rework
    Queue, not at entry time (CLAUDE.md) - the queue item must carry them."""
    case = _create_case(client, master_data, "Sanding / Surface", wo="WO-RC")
    client.patch(
        f"/api/v1/defect-cases/{case['id']}",
        json={"root_cause": "Dull sandpaper", "corrective_action": "Replace belt weekly"},
    )
    resp = client.get("/api/v1/rework-queue")
    row = next(r for r in resp.json() if r["work_order_number"] == "WO-RC")
    assert row["root_cause"] == "Dull sandpaper"
    assert row["corrective_action"] == "Replace belt weekly"
    assert row["repair_action"] is None


def test_trend_grouped_by_day_matches_pareto_and_summary_totals(client, master_data):
    _create_case(
        client, master_data, "Sanding / Surface", wo="WO-T1", qty=2, production_date="2026-07-20"
    )
    _create_case(
        client, master_data, "Dado / Bottom Groove", wo="WO-T2", qty=1, production_date="2026-07-21"
    )
    _create_case(client, master_data, "Other", wo="WO-T3", qty=3, production_date="2026-07-21")

    trend = client.get(
        "/api/v1/reports/trend",
        params={"start_date": "2026-07-20", "end_date": "2026-07-21", "group_by": "day"},
    ).json()

    by_period = {p["period"]: p for p in trend}
    assert by_period["2026-07-20"]["defect_events"] == 2
    assert by_period["2026-07-21"]["defect_events"] == 4

    summary = client.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-20", "end_date": "2026-07-21"}
    ).json()
    assert sum(p["defect_events"] for p in trend) == summary["defect_events"] == 6

    pareto = client.get(
        "/api/v1/reports/pareto", params={"start_date": "2026-07-20", "end_date": "2026-07-21"}
    ).json()
    assert sum(r["defect_events"] for r in pareto) == summary["defect_events"]


def test_trend_grouped_by_week_buckets_into_iso_weeks(client, master_data):
    # 2026-07-20 is a Monday (ISO week 30); 2026-07-21 is in the same ISO week.
    _create_case(
        client, master_data, "Sanding / Surface", wo="WO-W1", qty=1, production_date="2026-07-20"
    )
    _create_case(
        client, master_data, "Dado / Bottom Groove", wo="WO-W2", qty=2, production_date="2026-07-21"
    )

    trend = client.get(
        "/api/v1/reports/trend",
        params={"start_date": "2026-07-20", "end_date": "2026-07-21", "group_by": "week"},
    ).json()

    assert len(trend) == 1
    assert trend[0]["period"] == "2026-W30"
    assert trend[0]["defect_events"] == 3
