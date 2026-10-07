"""2026-10 redesign, phase 9: the Quality Today dashboard (one call) and its target."""

from __future__ import annotations

import datetime as dt

from app.services import quality_today_service

QT = "/api/v1/reports/quality-today"


def _case(client, master_data, day: str, **overrides):
    payload = {
        "production_date": day,
        "detected_at": f"{day}T14:30:00Z",
        "work_order_number": "179459",
        "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
        "priority": "Normal",
        "items": [{"defect_category_id": master_data["categories"]["Other"]}],
    }
    payload.update(overrides)
    resp = client.post("/api/v1/defect-cases", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_quality_today_shape_on_an_empty_database(client):
    body = client.get(QT).json()
    for key in (
        "today_stats",
        "this_week",
        "last_week",
        "trend",
        "top_defects",
        "kickbacks_by_day",
        "kickbacks_by_clamp",
        "oldest_open",
        "customer_issues",
        "feed_health",
        "target_per_100",
    ):
        assert key in body, key
    assert body["today_stats"]["defects_per_100"] is None  # zero inspected -> N/A, never /0
    assert body["target_per_100"] is None


def test_this_week_against_the_same_days_last_week(client, master_data):
    db = client.testing_sessionmaker()
    try:
        today = dt.date(2026, 10, 7)  # a Wednesday
        _case(client, master_data, "2026-10-06")
        _case(client, master_data, "2026-10-06")
        _case(client, master_data, "2026-09-29")
        _case(client, master_data, "2026-10-01")  # Thursday last week: outside the span
        body = quality_today_service.build(db, today)
    finally:
        db.close()
    assert (body["this_week"]["cases"], body["last_week"]["cases"]) == (2, 1)
    assert body["top_defects"] == [{"label": "Other", "count": 2, "delta": 1}]
    assert [o["work_order_number"] for o in body["oldest_open"]][:1] == ["179459"]


def test_target_is_saved_and_cleared(client):
    resp = client.put("/api/v1/settings/quality-target", json={"target_per_100": 3})
    assert resp.status_code == 200
    assert client.get(QT).json()["target_per_100"] == 3
    client.put("/api/v1/settings/quality-target", json={"target_per_100": None})
    assert client.get(QT).json()["target_per_100"] is None


def test_target_must_be_positive(client):
    assert (
        client.put("/api/v1/settings/quality-target", json={"target_per_100": 0}).status_code == 400
    )
