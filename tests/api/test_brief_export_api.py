"""API tests for the Brief Export endpoint (GET /api/v1/brief/summary).

Uses its own unauthenticated client fixture (like
tests/api/test_sync_ingest_api.py's unauth_client) rather than the shared
`client` fixture in tests/api/conftest.py, which pre-authenticates for every
other test - the whole point of this endpoint is that it works WITHOUT a login
session, gated instead by its own BRIEF_API_KEY header.
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
from app.models import (
    CustomerIssue,
    DailyProductionSummary,
    DailySchedule,
    DefectCase,
    DefectCategory,
    DefectItem,
    Station,
    SyncLog,
)
from app.seed_data import seed_master_data
from app.services import auth_service, defect_service

SUMMARY_PATH = "/api/v1/brief/summary"
TEST_BRIEF_KEY = "test-brief-key-do-not-use-in-prod"

# Same reference dates as tests/unit/test_brief_export_service.py: 2026-08-17
# is a Monday, 2026-08-21 the Friday of that week, 2026-08-24 the next Monday.
FRIDAY = dt.date(2026, 8, 21)
NEXT_MONDAY = dt.date(2026, 8, 24)


@pytest.fixture()
def brief_key(monkeypatch):
    """Sets BRIEF_API_KEY for the duration of a test and clears app.config's
    get_settings() lru_cache so the route (which calls get_settings() fresh
    per request) actually sees it, restoring the cache afterward."""
    monkeypatch.setenv("BRIEF_API_KEY", TEST_BRIEF_KEY)
    get_settings.cache_clear()
    yield TEST_BRIEF_KEY
    get_settings.cache_clear()


@pytest.fixture()
def unauth_client(brief_key):
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


def _seed_last_production_day(unauth_client) -> None:
    db = unauth_client.testing_sessionmaker()
    try:
        db.add(
            DailyProductionSummary(
                production_date=FRIDAY,
                shift="Day",
                drawers_inspected=390,
                drawers_rejected_unique=5,
            )
        )
        db.add(DailySchedule(production_date=FRIDAY, drawers_scheduled=406, source="sync"))
        db.commit()

        stations = {s.name: s.id for s in db.query(Station).all()}
        categories = {c.name: c.id for c in db.query(DefectCategory).all()}

        defect_service.create_defect_case(
            db,
            production_date=FRIDAY,
            detected_at=dt.datetime(2026, 8, 21, 9, 0, tzinfo=dt.timezone.utc),
            work_order_number="WO-9001",
            drawer_part_reference=None,
            found_station_id=stations["QC / Sorting / Shipping"],
            possible_source_station_id=None,
            priority="Normal",
            items=[
                {
                    "defect_category_id": categories["Sanding / Surface"],
                    "affected_drawer_quantity": 3,
                },
                {
                    "defect_category_id": categories["Dado / Bottom Groove"],
                    "affected_drawer_quantity": 1,
                },
            ],
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 200 with a valid key: full payload shape
# ---------------------------------------------------------------------------


def test_valid_key_returns_200_with_full_payload_shape(unauth_client, brief_key):
    _seed_last_production_day(unauth_client)

    resp = unauth_client.get(
        SUMMARY_PATH,
        params={"product": "drawers", "asof": NEXT_MONDAY.isoformat()},
        headers={"X-Brief-Key": brief_key},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["product"] == "drawers"
    assert body["asof"] == NEXT_MONDAY.isoformat()
    assert body["generated_at"]

    last_day = body["last_production_day"]
    assert last_day["date"] == FRIDAY.isoformat()
    assert last_day["entered"] is True
    assert last_day["inspected"] == 390
    assert last_day["scheduled_per_tracker"] == 406
    # Part 1: per-day defect figures, same date as last_production_day.
    assert last_day["cases"] == 1
    assert last_day["defect_events"] == 4

    week = body["week"]
    assert week["basis"] == "prior_full_week"
    assert week["cases"] == 1
    assert week["defect_events"] == 4
    assert isinstance(week["top_categories"], list)
    assert sum(c["count"] for c in week["top_categories"]) + week["other_count"] == 4

    # Part 2: day-by-day breakdown, one entry per working day in [start, end].
    days = week["days"]
    assert [d["date"] for d in days] == [
        dt.date(2026, 8, 17).isoformat(),  # Mon
        dt.date(2026, 8, 18).isoformat(),  # Tue
        dt.date(2026, 8, 19).isoformat(),  # Wed
        dt.date(2026, 8, 20).isoformat(),  # Thu
        FRIDAY.isoformat(),
    ]
    friday_entry = next(d for d in days if d["date"] == FRIDAY.isoformat())
    assert friday_entry["entered"] is True
    assert friday_entry["inspected"] == 390
    assert friday_entry["cases"] == 1
    # Every other day in this fixture has no summary row at all.
    monday_entry = next(d for d in days if d["date"] == dt.date(2026, 8, 17).isoformat())
    assert monday_entry["entered"] is False
    assert monday_entry["inspected"] is None
    assert monday_entry["cases"] == 0
    assert "scheduled" not in friday_entry  # the brief supplies its own
    assert sum(d["cases"] for d in days) == week["cases"]


# ---------------------------------------------------------------------------
# Auth: missing / wrong / unconfigured key
# ---------------------------------------------------------------------------


def test_missing_key_returns_401(unauth_client, brief_key):
    resp = unauth_client.get(SUMMARY_PATH, params={"product": "drawers"})
    assert resp.status_code == 401


def test_wrong_key_returns_401(unauth_client, brief_key):
    resp = unauth_client.get(
        SUMMARY_PATH,
        params={"product": "drawers"},
        headers={"X-Brief-Key": "totally-wrong"},
    )
    assert resp.status_code == 401


def test_key_rejected_when_brief_api_key_not_configured(unauth_client, monkeypatch):
    """If BRIEF_API_KEY is unset server-side, the endpoint must refuse every
    request, never silently accept one - see app/routers/brief.py
    _verify_brief_key."""
    monkeypatch.delenv("BRIEF_API_KEY", raising=False)
    get_settings.cache_clear()

    resp = unauth_client.get(
        SUMMARY_PATH,
        params={"product": "drawers"},
        headers={"X-Brief-Key": TEST_BRIEF_KEY},
    )

    assert resp.status_code == 401
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# product validation -> 400 in the standard error envelope
# ---------------------------------------------------------------------------


def test_product_doors_returns_400(unauth_client, brief_key):
    resp = unauth_client.get(
        SUMMARY_PATH,
        params={"product": "doors"},
        headers={"X-Brief-Key": brief_key},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["message"]
    assert body["error"]["field"] == "product"


def test_product_omitted_returns_400(unauth_client, brief_key):
    resp = unauth_client.get(SUMMARY_PATH, headers={"X-Brief-Key": brief_key})
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "product"


# ---------------------------------------------------------------------------
# Malformed asof -> 400 in the standard error envelope
# ---------------------------------------------------------------------------


def test_malformed_asof_returns_400(unauth_client, brief_key):
    resp = unauth_client.get(
        SUMMARY_PATH,
        params={"product": "drawers", "asof": "not-a-date"},
        headers={"X-Brief-Key": brief_key},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["message"]
    assert body["error"]["field"] == "asof"


# ---------------------------------------------------------------------------
# No session cookie required
# ---------------------------------------------------------------------------


def test_does_not_require_a_login_session(unauth_client, brief_key):
    assert auth_service.SESSION_COOKIE_NAME not in unauth_client.cookies

    resp = unauth_client.get(
        SUMMARY_PATH,
        params={"product": "drawers"},
        headers={"X-Brief-Key": brief_key},
    )

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Read-only: writes nothing to the database
# ---------------------------------------------------------------------------


def test_endpoint_writes_nothing(unauth_client, brief_key):
    _seed_last_production_day(unauth_client)

    def _row_counts(db):
        return {
            "daily_production_summaries": db.query(DailyProductionSummary).count(),
            "daily_schedules": db.query(DailySchedule).count(),
            "defect_cases": db.query(DefectCase).count(),
            "defect_items": db.query(DefectItem).count(),
            "sync_logs": db.query(SyncLog).count(),
            "customer_issues": db.query(CustomerIssue).count(),
        }

    db = unauth_client.testing_sessionmaker()
    try:
        before = _row_counts(db)
    finally:
        db.close()

    resp = unauth_client.get(
        SUMMARY_PATH,
        params={"product": "drawers", "asof": NEXT_MONDAY.isoformat()},
        headers={"X-Brief-Key": brief_key},
    )
    assert resp.status_code == 200
    # Part 2's day-by-day breakdown reads daily_production_summaries with an
    # extra grouped query - confirm that extra read still writes nothing.
    assert len(resp.json()["week"]["days"]) > 0

    db = unauth_client.testing_sessionmaker()
    try:
        after = _row_counts(db)
    finally:
        db.close()

    assert before == after
