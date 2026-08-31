"""API tests for the Working Days Logic (Part C) ingest guard: a sync payload
date that isn't a working day is rejected before it's written, while the manual
PUT escape hatch (overtime Saturdays) keeps working exactly as before.

Mirrors tests/api/test_schedule_sync_api.py's own unauthenticated client fixture
for the relay ingest endpoint (must work without a login session).
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
from app.models import DailySchedule
from app.seed_data import seed_master_data

INGEST_PATH = "/api/v1/sync/daily-schedule/ingest-raw"
TEST_RELAY_KEY = "test-relay-key-do-not-use-in-prod"

# 2026-08-22 is a Saturday, 2026-08-20 is a Thursday (plain weekdays/weekends,
# same fixed week used across tests/api/test_schedule_sync_api.py).
SATURDAY = "2026-08-22"
THURSDAY = "2026-08-20"


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


def test_sync_payload_with_a_saturday_is_skipped_and_no_row_is_created(unauth_client, relay_key):
    payload = {
        "schedules": [
            {"date": SATURDAY, "drawers_scheduled": 120},
            {"date": THURSDAY, "drawers_scheduled": 380},
        ]
    }

    resp = unauth_client.post(INGEST_PATH, json=payload, headers={"X-Relay-Key": relay_key})

    assert resp.status_code == 200
    body = resp.json()
    assert body["records_created"] == 1  # only the Thursday entry
    assert body["records_skipped"] == 1  # the Saturday entry
    assert "not a working day" in body["errors"]

    db = unauth_client.testing_sessionmaker()
    try:
        rows = {r.production_date.isoformat(): r.drawers_scheduled for r in db.query(DailySchedule)}
        assert rows == {THURSDAY: 380}
    finally:
        db.close()


def test_manual_put_for_the_same_saturday_still_applies(client):
    resp = client.put(
        "/api/v1/daily-production/schedule",
        json={"production_date": SATURDAY, "drawers_scheduled": 120},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["drawers_scheduled"] == 120
    assert body["source"] == "manual"

    get_resp = client.get("/api/v1/daily-production/schedule", params={"date": SATURDAY})
    schedules = get_resp.json()["schedules"]
    assert len(schedules) == 1
    assert schedules[0]["source"] == "manual"
