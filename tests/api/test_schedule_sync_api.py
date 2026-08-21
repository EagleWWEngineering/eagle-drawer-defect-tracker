"""API tests for the Phase 6 relay ingest endpoint
(POST /api/v1/sync/daily-schedule/ingest-raw).

Mirrors tests/api/test_sync_ingest_api.py's structure for the Phase 3 customer-
issues ingest endpoint: its own unauthenticated client fixture (this endpoint must
work WITHOUT a login session, gated instead by RELAY_API_KEY), same auth/shape
assertions.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.database import Base
from app.dependencies import get_db
from app.main import app
from app.models import DailySchedule, SyncLog
from app.seed_data import seed_master_data
from app.services import auth_service, schedule_service

INGEST_PATH = "/api/v1/sync/daily-schedule/ingest-raw"
TEST_RELAY_KEY = "test-relay-key-do-not-use-in-prod"


@pytest.fixture()
def relay_key(monkeypatch):
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
    try:
        yield test_client
    finally:
        test_client.close()
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Valid key + valid payload
# ---------------------------------------------------------------------------


def test_ingest_valid_key_and_payload_creates_rows(unauth_client, relay_key):
    payload = {
        "schedules": [
            {"date": "2026-08-20", "drawers_scheduled": 431},
            {"date": "2026-08-21", "drawers_scheduled": 406},
        ]
    }

    resp = unauth_client.post(INGEST_PATH, json=payload, headers={"X-Relay-Key": relay_key})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["records_created"] == 2
    assert body["source_url"].startswith("relay:")

    db = unauth_client.testing_sessionmaker()
    try:
        rows = {r.production_date.isoformat(): r.drawers_scheduled for r in db.query(DailySchedule)}
        assert rows == {"2026-08-20": 431, "2026-08-21": 406}
    finally:
        db.close()


def test_ingest_second_call_updates_rather_than_duplicates(unauth_client, relay_key):
    first = {"schedules": [{"date": "2026-08-20", "drawers_scheduled": 431}]}
    unauth_client.post(INGEST_PATH, json=first, headers={"X-Relay-Key": relay_key})

    second = {"schedules": [{"date": "2026-08-20", "drawers_scheduled": 440}]}
    resp = unauth_client.post(INGEST_PATH, json=second, headers={"X-Relay-Key": relay_key})

    assert resp.status_code == 200
    body = resp.json()
    assert body["records_created"] == 0
    assert body["records_updated"] == 1

    db = unauth_client.testing_sessionmaker()
    try:
        assert db.query(DailySchedule).count() == 1
        assert db.query(DailySchedule).first().drawers_scheduled == 440
    finally:
        db.close()


def test_ingest_null_drawers_scheduled_is_skipped(unauth_client, relay_key):
    payload = {"schedules": [{"date": "2026-08-20", "drawers_scheduled": None}]}

    resp = unauth_client.post(INGEST_PATH, json=payload, headers={"X-Relay-Key": relay_key})

    assert resp.status_code == 200
    body = resp.json()
    assert body["records_skipped"] == 1
    assert body["records_created"] == 0

    db = unauth_client.testing_sessionmaker()
    try:
        assert db.query(DailySchedule).count() == 0
    finally:
        db.close()


def test_ingest_never_overwrites_a_manual_row(unauth_client, relay_key):
    db = unauth_client.testing_sessionmaker()
    try:
        schedule_service.upsert_schedule(
            db, production_date=dt.date(2026, 8, 20), drawers_scheduled=999, source="manual"
        )
    finally:
        db.close()

    payload = {"schedules": [{"date": "2026-08-20", "drawers_scheduled": 431}]}
    resp = unauth_client.post(INGEST_PATH, json=payload, headers={"X-Relay-Key": relay_key})

    assert resp.status_code == 200
    assert resp.json()["records_skipped"] == 1

    db = unauth_client.testing_sessionmaker()
    try:
        row = db.query(DailySchedule).first()
        assert row.drawers_scheduled == 999
        assert row.source == "manual"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Auth: missing / wrong key
# ---------------------------------------------------------------------------


def test_ingest_missing_key_returns_401_and_processes_nothing(unauth_client, relay_key):
    payload = {"schedules": [{"date": "2026-08-20", "drawers_scheduled": 431}]}

    resp = unauth_client.post(INGEST_PATH, json=payload)

    assert resp.status_code == 401
    db = unauth_client.testing_sessionmaker()
    try:
        assert db.query(DailySchedule).count() == 0
        assert db.query(SyncLog).count() == 0
    finally:
        db.close()


def test_ingest_wrong_key_returns_401_and_processes_nothing(unauth_client, relay_key):
    payload = {"schedules": [{"date": "2026-08-20", "drawers_scheduled": 431}]}

    resp = unauth_client.post(INGEST_PATH, json=payload, headers={"X-Relay-Key": "totally-wrong"})

    assert resp.status_code == 401
    db = unauth_client.testing_sessionmaker()
    try:
        assert db.query(DailySchedule).count() == 0
        assert db.query(SyncLog).count() == 0
    finally:
        db.close()


def test_ingest_key_rejected_when_relay_api_key_not_configured(unauth_client, monkeypatch):
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    get_settings.cache_clear()

    resp = unauth_client.post(
        INGEST_PATH, json={"schedules": []}, headers={"X-Relay-Key": TEST_RELAY_KEY}
    )

    assert resp.status_code == 401
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Malformed payload -> clean 400, never a 500, no sync_logs row either
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_payload",
    [
        {},
        {"count": 0},
        {"schedules": "not-a-list"},
        {"schedules": None},
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


def test_ingest_bad_individual_entry_does_not_fail_the_whole_request(unauth_client, relay_key):
    """A malformed shape overall is a clean 400 (above); a bad individual entry
    within an otherwise well-shaped payload is skipped-and-counted instead, per
    process_schedule_payload - this must still return 200."""
    payload = {
        "schedules": [
            {"date": "not-a-real-date", "drawers_scheduled": 100},
            {"date": "2026-08-20", "drawers_scheduled": 431},
        ]
    }
    resp = unauth_client.post(INGEST_PATH, json=payload, headers={"X-Relay-Key": relay_key})

    assert resp.status_code == 200
    body = resp.json()
    assert body["records_skipped"] == 1
    assert body["records_created"] == 1


# ---------------------------------------------------------------------------
# No login session required
# ---------------------------------------------------------------------------


def test_ingest_does_not_require_a_login_session(unauth_client, relay_key):
    assert auth_service.SESSION_COOKIE_NAME not in unauth_client.cookies

    resp = unauth_client.post(
        INGEST_PATH, json={"schedules": []}, headers={"X-Relay-Key": relay_key}
    )

    assert resp.status_code == 200
