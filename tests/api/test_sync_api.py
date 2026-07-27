"""API tests for /api/v1/sync/* (Phase 3). HTTP calls to the production brief are
mocked - no real network access is required or attempted."""

from __future__ import annotations

import httpx

from app.services import sync_service


def _issue_payload(**overrides) -> dict:
    payload = {
        "thread_id": "thread-api-1",
        "day": "2026-07-25",
        "customer": "Armadio IQC",
        "order_no": "SO-1001",
        "summary": "Drawer fronts off-dimension.",
        "category": "manufacturing",
        "subcategory": "wrong size",
        "station": "QA/Final",
        "rework_cost": 300,
        "cost_note": "3 pc x $100 base rate",
        "photos_json": '["url1"]',
        "needs_review": False,
        "ignored": False,
    }
    payload.update(overrides)
    return payload


def _mock_brief(monkeypatch, handler) -> None:
    def fake_build_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://fake-brief"
        )

    monkeypatch.setattr(sync_service, "_build_client", fake_build_client)


def test_status_returns_null_when_never_synced(client):
    resp = client.get("/api/v1/sync/status")
    assert resp.status_code == 200
    assert resp.json() is None


def test_trigger_sync_creates_issue_and_returns_result(client, monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "count": 1, "issues": [_issue_payload()]})

    _mock_brief(monkeypatch, handler)

    resp = client.post("/api/v1/sync/customer-issues")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["records_created"] == 1

    list_resp = client.get("/api/v1/customer-issues", params={"order_number": "SO-1001"})
    assert list_resp.json()["total"] == 1
    synced_issue = list_resp.json()["issues"][0]
    assert synced_issue["is_synced"] is True


def test_trigger_sync_failure_returns_200_with_failed_status(client, monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _mock_brief(monkeypatch, handler)

    resp = client.post("/api/v1/sync/customer-issues")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert "Could not reach" in body["errors"]


def test_status_reflects_most_recent_sync(client, monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "count": 0, "issues": []})

    _mock_brief(monkeypatch, handler)
    client.post("/api/v1/sync/customer-issues")

    resp = client.get("/api/v1/sync/status")
    body = resp.json()
    assert body["status"] == "success"
    assert body["records_fetched"] == 0


def test_logs_lists_most_recent_first_and_respects_limit(client, monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "count": 0, "issues": []})

    _mock_brief(monkeypatch, handler)
    for _ in range(3):
        client.post("/api/v1/sync/customer-issues")

    resp = client.get("/api/v1/sync/logs", params={"limit": 2})
    assert resp.status_code == 200
    logs = resp.json()
    assert len(logs) == 2
    assert logs[0]["id"] > logs[1]["id"]


def test_manually_created_issue_is_not_flagged_as_synced(client, customer_categories):
    payload = {
        "reported_date": "2026-07-24",
        "customer_name": "Walk-in customer",
        "issue_category_id": customer_categories["Other"],
        "source_type": "Manufacturing",
        "piece_count": 1,
        "description": "Reported by phone.",
    }
    resp = client.post("/api/v1/customer-issues", json=payload)
    assert resp.status_code == 200
    assert resp.json()["is_synced"] is False
