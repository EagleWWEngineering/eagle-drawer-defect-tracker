"""2026-10 redesign, phase 8: "Unique drawers rejected" is automatic.

A row created by the production-count feed starts rejected_source='auto': the
reports use the live count of that day's cases (kickbacks included), so the
rejection rate and first-pass yield are right all day. Saving a number on Daily
Summary makes it 'manual' (the typed number wins); "Use automatic" switches back.
"""

from __future__ import annotations

import pytest

from app.config import get_settings

TEST_RELAY_KEY = "test-relay-key-do-not-use-in-prod"
DAY = "2026-10-05"
RANGE = {"start_date": DAY, "end_date": DAY}


@pytest.fixture()
def fed_day(client, monkeypatch, master_data):
    monkeypatch.setenv("RELAY_API_KEY", TEST_RELAY_KEY)
    get_settings.cache_clear()
    resp = client.post(
        "/api/v1/sync/daily-completed/ingest-raw",
        json={
            "source": "eagle-drawers-production-count",
            "station": "QC_SORTING",
            "counts": {DAY: 200},
        },
        headers={"X-Relay-Key": TEST_RELAY_KEY},
    )
    assert resp.status_code == 200, resp.text
    for _ in range(4):
        created = client.post(
            "/api/v1/defect-cases",
            json={
                "production_date": DAY,
                "detected_at": f"{DAY}T14:30:00Z",
                "work_order_number": "179459",
                "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
                "priority": "Normal",
                "items": [{"defect_category_id": master_data["categories"]["Other"]}],
            },
        )
        assert created.status_code == 200, created.text
    yield
    get_settings.cache_clear()


def _summary(client):
    return client.get("/api/v1/reports/summary", params=RANGE).json()


def test_a_fed_day_counts_its_cases_automatically(client, fed_day):
    kpis = _summary(client)
    assert kpis["unique_drawers_rejected"] == 4
    assert kpis["rejection_rate"] == pytest.approx(2.0)
    row = client.get("/api/v1/daily-production", params=RANGE).json()[0]
    assert (row["rejected_source"], row["effective_rejected"]) == ("auto", 4)


def test_a_saved_number_wins(client, fed_day):
    resp = client.put(
        f"/api/v1/daily-production/{DAY}", json={"shift": "Day", "drawers_rejected_unique": 3}
    )
    assert resp.status_code == 200, resp.text
    assert _summary(client)["unique_drawers_rejected"] == 3
    row = client.get("/api/v1/daily-production", params=RANGE).json()[0]
    assert (row["rejected_source"], row["effective_rejected"]) == ("manual", 3)


def test_use_automatic_switches_back(client, fed_day):
    client.put(
        f"/api/v1/daily-production/{DAY}", json={"shift": "Day", "drawers_rejected_unique": 3}
    )
    resp = client.post(f"/api/v1/daily-production/{DAY}/use-automatic-rejected")
    assert resp.status_code == 200, resp.text
    assert resp.json()["effective_rejected"] == 4
    assert _summary(client)["unique_drawers_rejected"] == 4


def test_the_trend_uses_the_automatic_count_too(client, fed_day):
    point = client.get("/api/v1/reports/trend", params=RANGE).json()[0]
    assert point["unique_drawers_rejected"] == 4


def test_use_automatic_on_a_missing_day_is_404(client):
    assert (
        client.post("/api/v1/daily-production/2026-01-01/use-automatic-rejected").status_code == 404
    )
