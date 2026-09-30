"""PROJECT_SPEC_PHASE10.md Part 1: with drawers_inspected fed by production
count and rejected counted from cases, rejected > inspected and inspected == 0
are both normal - every rate-bearing endpoint must still answer 200 with
sane (never negative, never divide-by-zero) values."""

from __future__ import annotations

import pytest


@pytest.fixture()
def reports_client(client, master_data, feed_completed):
    """A day with 3 rejected cases but only 1 fed completed drawer, plus a day
    with cases and no feed row at all (inspected = 0)."""
    feed_completed({"2026-07-24": 1})
    for wo in ("WO-1", "WO-2", "WO-3"):
        for date in ("2026-07-24", "2026-07-23"):
            client.post(
                "/api/v1/defect-cases",
                json={
                    "production_date": date,
                    "detected_at": f"{date}T14:30:00Z",
                    "work_order_number": wo,
                    "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
                    "priority": "Normal",
                    "items": [
                        {"defect_category_id": master_data["categories"]["Sanding / Surface"]}
                    ],
                },
            )
    client.put(
        "/api/v1/daily-production/2026-07-24",
        json={"shift": "Day", "drawers_rejected_unique": 3},
    )
    return client


def test_reports_and_dashboard_endpoints_survive_rejected_gt_inspected(reports_client):
    c = reports_client
    summary = c.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-24", "end_date": "2026-07-24"}
    )
    assert summary.status_code == 200
    assert summary.json()["first_pass_yield"] == 0.0
    assert summary.json()["rejection_rate"] == 300.0

    zero = c.get(
        "/api/v1/reports/summary", params={"start_date": "2026-07-23", "end_date": "2026-07-23"}
    )
    assert zero.status_code == 200
    assert zero.json()["rejection_rate"] is None

    for path, params in [
        ("/api/v1/reports/trend", {"start_date": "2026-07-20", "end_date": "2026-07-24"}),
        (
            "/api/v1/daily-production/schedule-attainment",
            {"start_date": "2026-07-20", "end_date": "2026-07-24"},
        ),
        ("/api/v1/daily-production", {}),
        ("/api/v1/customer-issues/summary", {"start_date": "2026-07-20", "end_date": "2026-07-24"}),
    ]:
        assert c.get(path, params=params).status_code == 200, path
