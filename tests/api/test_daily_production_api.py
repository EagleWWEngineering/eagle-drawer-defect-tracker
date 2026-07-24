"""API tests for the Daily Production Summary endpoint."""

from __future__ import annotations


def test_upsert_and_read_back(client):
    resp = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 100,
            "drawers_rejected_unique": 10,
            "drawers_reworked": 7,
            "drawers_scrapped": 2,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["drawers_inspected"] == 100
    assert body["warnings"] == []

    list_resp = client.get("/api/v1/daily-production")
    assert len(list_resp.json()) == 1


def test_hard_rule_rejected_gt_inspected_returns_400(client):
    resp = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 5,
            "drawers_rejected_unique": 6,
            "drawers_reworked": 0,
            "drawers_scrapped": 0,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "drawers_rejected_unique"


def test_soft_warning_without_note_is_rejected_with_note_is_allowed(client):
    resp = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 50,
            "drawers_rejected_unique": 0,
            "drawers_reworked": 3,
            "drawers_scrapped": 0,
        },
    )
    assert resp.status_code == 400

    resp2 = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 50,
            "drawers_rejected_unique": 0,
            "drawers_reworked": 3,
            "drawers_scrapped": 0,
            "notes": "Reworked yesterday's rejects.",
        },
    )
    assert resp2.status_code == 200
    assert len(resp2.json()["warnings"]) >= 1


def test_same_date_and_shift_upserts_not_duplicates(client):
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
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 120,
            "drawers_rejected_unique": 12,
            "drawers_reworked": 6,
            "drawers_scrapped": 3,
        },
    )
    rows = client.get("/api/v1/daily-production").json()
    assert len(rows) == 1
    assert rows[0]["drawers_inspected"] == 120
