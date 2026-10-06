"""2026-10 redesign, phase 10: feed health (GET /api/v1/sync/health)."""

from __future__ import annotations

import datetime as dt

import pytest

from app.config import get_settings
from app.models import FeedStatus
from app.services import feed_health_service

KEY = "test-relay-key-do-not-use-in-prod"
HEADERS = {"X-Relay-Key": KEY}


@pytest.fixture()
def relay_key(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _states(client) -> dict[str, str]:
    return {f["feed"]: f["state"] for f in client.get("/api/v1/sync/health").json()["feeds"]}


def test_nothing_received_yet_is_never(client):
    body = client.get("/api/v1/sync/health").json()
    assert body["ok"] is True
    assert set(_states(client).values()) == {"never"}


def test_an_accepted_push_is_ok(client, relay_key):
    client.post("/api/v1/sync/order-lines/ingest-raw", json={"orders": {}}, headers=HEADERS)
    client.get("/api/v1/sync/customer-issues/relay-status", headers=HEADERS)
    states = _states(client)
    assert states["order_lines"] == "ok"
    assert states["heartbeat"] == "ok"


def test_a_refused_push_is_an_error_with_the_reason(client, relay_key):
    resp = client.post(
        "/api/v1/sync/order-lines/ingest-raw", json={"orders": "nope"}, headers=HEADERS
    )
    assert resp.status_code == 422
    body = client.get("/api/v1/sync/health").json()
    row = next(f for f in body["feeds"] if f["feed"] == "order_lines")
    assert row["state"] == "error"
    assert "refused" in row["message"]
    assert body["ok"] is False


def test_a_feed_past_its_schedule_is_late(client, relay_key):
    client.get("/api/v1/sync/customer-issues/relay-status", headers=HEADERS)
    db = client.testing_sessionmaker()
    try:
        row = db.get(FeedStatus, "heartbeat")
        row.last_received_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)
        db.commit()
        status = feed_health_service.status(db)
    finally:
        db.close()
    assert status["ok"] is False
    assert next(f for f in status["feeds"] if f["feed"] == "heartbeat")["state"] == "late"


def test_quiet_kickback_feed_is_never_late(client, relay_key):
    client.post(
        "/api/v1/sync/drawer-events/ingest-raw",
        json={"source": "eagle-drawers-production-count", "events": []},
        headers=HEADERS,
    )
    db = client.testing_sessionmaker()
    try:
        row = db.get(FeedStatus, "drawer_events")
        row.last_received_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
        db.commit()
        status = feed_health_service.status(db)
    finally:
        db.close()
    assert next(f for f in status["feeds"] if f["feed"] == "drawer_events")["state"] == "ok"
