"""API tests for the Phase 3 relay ingest endpoint
(POST /api/v1/sync/customer-issues/ingest-raw).

Uses its own unauthenticated client fixture (like tests/api/test_auth_api.py's
raw_client) rather than the shared `client` fixture in tests/api/conftest.py, which
pre-authenticates for every other test - the whole point of this endpoint is that it
works WITHOUT a login session, gated instead by a separate RELAY_API_KEY header.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.database import Base
from app.dependencies import get_db
from app.main import app
from app.models import CustomerIssue, SyncLog
from app.seed_data import seed_master_data
from app.services import auth_service

INGEST_PATH = "/api/v1/sync/customer-issues/ingest-raw"
TEST_RELAY_KEY = "test-relay-key-do-not-use-in-prod"


@pytest.fixture()
def relay_key(monkeypatch):
    """Sets RELAY_API_KEY for the duration of a test and clears app.config's
    get_settings() lru_cache so the route (which calls get_settings() fresh per
    request) actually sees it, restoring the cache afterward so later tests recompute
    settings from the real, monkeypatch-restored environment."""
    monkeypatch.setenv("RELAY_API_KEY", TEST_RELAY_KEY)
    get_settings.cache_clear()
    yield TEST_RELAY_KEY
    get_settings.cache_clear()


@pytest.fixture()
def unauth_client(relay_key):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    seed_session = TestingSession()
    seed_master_data(seed_session)
    seed_session.close()

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    test_client.testing_sessionmaker = TestingSession
    # Deliberately no session cookie is ever set on this client.
    try:
        yield test_client
    finally:
        test_client.close()
        app.dependency_overrides.clear()


def _issue_payload(**overrides) -> dict:
    payload = {
        "thread_id": "thread-1",
        "day": "2026-07-25",
        "customer": "Armadio IQC",
        "order_no": None,
        "summary": "Drawer fronts off-dimension.",
        "category": "manufacturing",
        "subcategory": "wrong size",
        "station": "QA/Final",
        "rework_cost": 300,
        "cost_note": "3 pc x $100 base rate",
        "photos_json": '["url1"]',
        "confidence": 0.92,
        "needs_review": False,
        "ignored": False,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Valid key + valid payload
# ---------------------------------------------------------------------------


def test_ingest_valid_key_and_payload_creates_issue(unauth_client, relay_key):
    payload = {"ok": True, "count": 1, "issues": [_issue_payload()]}

    resp = unauth_client.post(INGEST_PATH, json=payload, headers={"X-Relay-Key": relay_key})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["records_created"] == 1
    assert body["records_updated"] == 0
    assert body["source_url"].startswith("relay:")

    db = unauth_client.testing_sessionmaker()
    try:
        issue = db.query(CustomerIssue).filter(CustomerIssue.source_thread_id == "thread-1").first()
        assert issue is not None
        assert issue.customer_name == "Armadio IQC"
    finally:
        db.close()


def test_ingest_second_call_updates_rather_than_duplicates(unauth_client, relay_key):
    first = {"issues": [_issue_payload()]}
    unauth_client.post(INGEST_PATH, json=first, headers={"X-Relay-Key": relay_key})

    second = {"issues": [_issue_payload(customer="Armadio IQC (renamed)")]}
    resp = unauth_client.post(INGEST_PATH, json=second, headers={"X-Relay-Key": relay_key})

    assert resp.status_code == 200
    body = resp.json()
    assert body["records_created"] == 0
    assert body["records_updated"] == 1

    db = unauth_client.testing_sessionmaker()
    try:
        count = db.query(CustomerIssue).filter(CustomerIssue.source_thread_id == "thread-1").count()
        assert count == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Auth: missing / wrong key
# ---------------------------------------------------------------------------


def test_ingest_missing_key_returns_401_and_processes_nothing(unauth_client, relay_key):
    payload = {"issues": [_issue_payload()]}

    resp = unauth_client.post(INGEST_PATH, json=payload)

    assert resp.status_code == 401
    db = unauth_client.testing_sessionmaker()
    try:
        assert db.query(CustomerIssue).count() == 0
        assert db.query(SyncLog).count() == 0
    finally:
        db.close()


def test_ingest_wrong_key_returns_401_and_processes_nothing(unauth_client, relay_key):
    payload = {"issues": [_issue_payload()]}

    resp = unauth_client.post(INGEST_PATH, json=payload, headers={"X-Relay-Key": "totally-wrong"})

    assert resp.status_code == 401
    db = unauth_client.testing_sessionmaker()
    try:
        assert db.query(CustomerIssue).count() == 0
        assert db.query(SyncLog).count() == 0
    finally:
        db.close()


def test_ingest_key_rejected_when_relay_api_key_not_configured(unauth_client, monkeypatch):
    """If RELAY_API_KEY is unset server-side, the endpoint must refuse every
    request, never silently accept one - see app/routers/sync.py _verify_relay_key."""
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    get_settings.cache_clear()

    resp = unauth_client.post(
        INGEST_PATH, json={"issues": []}, headers={"X-Relay-Key": TEST_RELAY_KEY}
    )

    assert resp.status_code == 401
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Malformed payload -> clean 400, never a 500
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_payload",
    [
        {},
        {"count": 0},
        {"issues": "not-a-list"},
        {"issues": None},
    ],
)
def test_ingest_malformed_payload_returns_400_and_processes_nothing(
    unauth_client, relay_key, bad_payload
):
    resp = unauth_client.post(INGEST_PATH, json=bad_payload, headers={"X-Relay-Key": relay_key})

    assert resp.status_code == 400
    assert resp.json()["error"]["message"]

    db = unauth_client.testing_sessionmaker()
    try:
        assert db.query(SyncLog).count() == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# No login session required
# ---------------------------------------------------------------------------


def test_ingest_does_not_require_a_login_session(unauth_client, relay_key):
    assert auth_service.SESSION_COOKIE_NAME not in unauth_client.cookies

    resp = unauth_client.post(INGEST_PATH, json={"issues": []}, headers={"X-Relay-Key": relay_key})

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# ingest-raw clears a pending manual sync request on success
# ---------------------------------------------------------------------------


def test_ingest_success_clears_pending_manual_sync_request(unauth_client, relay_key):
    from app.services import sync_service

    db = unauth_client.testing_sessionmaker()
    try:
        sync_service.request_manual_sync(db)
        assert sync_service.is_manual_sync_pending(db) is True
    finally:
        db.close()

    resp = unauth_client.post(INGEST_PATH, json={"issues": []}, headers={"X-Relay-Key": relay_key})
    assert resp.status_code == 200

    db = unauth_client.testing_sessionmaker()
    try:
        assert sync_service.is_manual_sync_pending(db) is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/v1/sync/customer-issues/relay-status (the local relay's frequent
# heartbeat check-in) - same auth pattern as ingest-raw: RELAY_API_KEY header,
# no login session required.
# ---------------------------------------------------------------------------

RELAY_STATUS_PATH = "/api/v1/sync/customer-issues/relay-status"


def test_relay_status_missing_key_returns_401(unauth_client, relay_key):
    resp = unauth_client.get(RELAY_STATUS_PATH)
    assert resp.status_code == 401


def test_relay_status_wrong_key_returns_401(unauth_client, relay_key):
    resp = unauth_client.get(RELAY_STATUS_PATH, headers={"X-Relay-Key": "totally-wrong"})
    assert resp.status_code == 401


def test_relay_status_does_not_require_a_login_session(unauth_client, relay_key):
    assert auth_service.SESSION_COOKIE_NAME not in unauth_client.cookies

    resp = unauth_client.get(RELAY_STATUS_PATH, headers={"X-Relay-Key": relay_key})

    assert resp.status_code == 200


def test_relay_status_updates_heartbeat_and_reports_not_pending(unauth_client, relay_key):
    from app.services import sync_service

    resp = unauth_client.get(RELAY_STATUS_PATH, headers={"X-Relay-Key": relay_key})

    assert resp.status_code == 200
    assert resp.json()["manual_sync_pending"] is False

    db = unauth_client.testing_sessionmaker()
    try:
        assert sync_service.get_relay_last_seen_at(db) is not None
    finally:
        db.close()


def test_relay_status_reports_pending_when_manual_sync_requested(unauth_client, relay_key):
    from app.services import sync_service

    db = unauth_client.testing_sessionmaker()
    try:
        sync_service.request_manual_sync(db)
    finally:
        db.close()

    resp = unauth_client.get(RELAY_STATUS_PATH, headers={"X-Relay-Key": relay_key})

    assert resp.status_code == 200
    assert resp.json()["manual_sync_pending"] is True
