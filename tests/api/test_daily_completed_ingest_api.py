"""API tests for PROJECT_SPEC_PHASE10.md Part 1: the production-count "drawers
completed" feed (POST /api/v1/sync/daily-completed/ingest-raw) - the only
writer of DailyProductionSummary.drawers_inspected.

Own unauthenticated client fixture (like tests/api/test_schedule_sync_api.py):
this endpoint must work WITHOUT a login session, gated by RELAY_API_KEY. The
brief key is set too so the same client can read GET /api/v1/brief/summary
back and prove the fed value reaches the production brief unchanged.
"""

from __future__ import annotations

import datetime as dt
import decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.database import Base
from app.dependencies import get_db
from app.main import app
from app.models import DailyProductionSummary, SyncLog
from app.seed_data import seed_master_data
from app.services import auth_service

INGEST_PATH = "/api/v1/sync/daily-completed/ingest-raw"
TEST_RELAY_KEY = "test-relay-key-do-not-use-in-prod"
TEST_BRIEF_KEY = "test-brief-key-do-not-use-in-prod"


def _body(counts: dict) -> dict:
    """The exact shape production count sends."""
    return {
        "source": "eagle-drawers-production-count",
        "station": "QC_SORTING",
        "generated_at": "2026-09-30T19:30:00Z",
        "counts": counts,
    }


@pytest.fixture()
def keys(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", TEST_RELAY_KEY)
    monkeypatch.setenv("BRIEF_API_KEY", TEST_BRIEF_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def unauth_client(keys):
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


def _post(client, counts: dict, key: str | None = TEST_RELAY_KEY):
    headers = {"X-Relay-Key": key} if key is not None else {}
    return client.post(INGEST_PATH, json=_body(counts), headers=headers)


def _rows(client) -> dict[tuple[dt.date, str], DailyProductionSummary]:
    db = client.testing_sessionmaker()
    try:
        rows = db.query(DailyProductionSummary).all()
        db.expunge_all()
        return {(r.production_date, r.shift): r for r in rows}
    finally:
        db.close()


def _snapshot(client) -> list[tuple]:
    return sorted(
        (
            r.production_date,
            r.shift,
            r.drawers_inspected,
            r.drawers_rejected_unique,
            r.drawers_reworked,
            r.drawers_scrapped,
            r.notes,
            r.cost_per_drawer_at_time,
        )
        for r in _rows(client).values()
    )


def _login(client) -> None:
    db = client.testing_sessionmaker()
    token = auth_service.create_session(db)
    db.close()
    client.cookies.set(auth_service.SESSION_COOKIE_NAME, token)


# ---------------------------------------------------------------------------
# Upsert rules
# ---------------------------------------------------------------------------


def test_creates_rows_for_nonzero_counts_and_skips_zero_with_no_row(unauth_client):
    resp = _post(unauth_client, {"2026-09-26": 0, "2026-09-28": 171, "2026-09-29": 57})
    assert resp.status_code == 200
    assert resp.json() == {"received": 3, "updated": 0, "created": 2, "skipped_zero": 1}

    rows = _rows(unauth_client)
    assert set(rows) == {(dt.date(2026, 9, 28), "Day"), (dt.date(2026, 9, 29), "Day")}
    row = rows[(dt.date(2026, 9, 28), "Day")]
    assert row.drawers_inspected == 171
    assert row.drawers_rejected_unique == 0
    assert row.drawers_reworked == 0
    assert row.drawers_scrapped == 0
    assert row.notes is None
    # Snapshotted the same way a form-created row is (seeded default rate).
    assert row.cost_per_drawer_at_time == decimal.Decimal("35.00")


def test_update_sets_only_drawers_inspected_and_leaves_every_other_column(unauth_client):
    db = unauth_client.testing_sessionmaker()
    db.add(
        DailyProductionSummary(
            production_date=dt.date(2026, 9, 29),
            shift="Day",
            drawers_inspected=90,  # an old hand-typed value
            drawers_rejected_unique=7,
            drawers_reworked=3,
            drawers_scrapped=1,
            notes="typed by hand",
            cost_per_drawer_at_time=decimal.Decimal("40.00"),
        )
    )
    db.commit()
    db.close()

    resp = _post(unauth_client, {"2026-09-29": 0})
    assert resp.json() == {"received": 1, "updated": 1, "created": 0, "skipped_zero": 0}

    row = _rows(unauth_client)[(dt.date(2026, 9, 29), "Day")]
    # A zero on an EXISTING row is a real value (e.g. every scan undone) - applied.
    assert row.drawers_inspected == 0
    assert row.drawers_rejected_unique == 7
    assert row.drawers_reworked == 3
    assert row.drawers_scrapped == 1
    assert row.notes == "typed by hand"
    assert row.cost_per_drawer_at_time == decimal.Decimal("40.00")

    _post(unauth_client, {"2026-09-29": 144})
    assert _rows(unauth_client)[(dt.date(2026, 9, 29), "Day")].drawers_inspected == 144


def test_only_the_day_shift_row_is_touched(unauth_client):
    db = unauth_client.testing_sessionmaker()
    db.add(
        DailyProductionSummary(
            production_date=dt.date(2026, 9, 29), shift="Night", drawers_inspected=20
        )
    )
    db.commit()
    db.close()

    _post(unauth_client, {"2026-09-29": 100})
    rows = _rows(unauth_client)
    assert rows[(dt.date(2026, 9, 29), "Night")].drawers_inspected == 20
    assert rows[(dt.date(2026, 9, 29), "Day")].drawers_inspected == 100


def test_same_body_twice_is_idempotent(unauth_client):
    counts = {f"2026-09-{d}": n for d, n in [(24, 0), (25, 171), (26, 0), (29, 60), (30, 57)]}
    first = _post(unauth_client, counts)
    state_after_first = _snapshot(unauth_client)
    second = _post(unauth_client, counts)

    assert first.status_code == second.status_code == 200
    assert _snapshot(unauth_client) == state_after_first
    # Second time round the created rows already exist - they're updates now.
    assert second.json() == {"received": 5, "updated": 3, "created": 0, "skipped_zero": 2}


def test_trailing_resend_applies_a_correction(unauth_client):
    _post(unauth_client, {"2026-09-29": 60})
    _post(unauth_client, {"2026-09-29": 58, "2026-09-30": 12})  # a QC undo on the 29th
    rows = _rows(unauth_client)
    assert rows[(dt.date(2026, 9, 29), "Day")].drawers_inspected == 58
    assert rows[(dt.date(2026, 9, 30), "Day")].drawers_inspected == 12


def test_saturday_count_is_recorded_not_rejected(unauth_client):
    """Unlike the schedule ingest, a count > 0 on a weekend is real overtime QC
    scans - recorded."""
    resp = _post(unauth_client, {"2026-09-26": 40})  # a Saturday
    assert resp.json()["created"] == 1


def test_each_call_writes_one_sync_log_row(unauth_client):
    _post(unauth_client, {"2026-09-29": 60, "2026-09-27": 0})
    db = unauth_client.testing_sessionmaker()
    logs = db.query(SyncLog).all()
    db.close()
    assert len(logs) == 1
    log = logs[0]
    assert log.status == "success"
    assert log.source_url.startswith("relay:eagle-drawers-production-count")
    assert (log.records_fetched, log.records_created, log.records_updated) == (2, 1, 0)
    assert log.records_skipped == 1


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", [None, "wrong-key"])
def test_missing_or_wrong_relay_key_is_rejected_and_writes_nothing(unauth_client, key):
    resp = _post(unauth_client, {"2026-09-29": 60}, key=key)
    assert resp.status_code == 401
    assert _rows(unauth_client) == {}
    db = unauth_client.testing_sessionmaker()
    assert db.query(SyncLog).count() == 0
    db.close()


def test_missing_key_with_no_body_is_still_401_not_422(unauth_client):
    assert unauth_client.post(INGEST_PATH).status_code == 401


def test_works_without_a_login_session(unauth_client):
    assert not unauth_client.cookies
    assert _post(unauth_client, {"2026-09-29": 1}).status_code == 200


# ---------------------------------------------------------------------------
# Validation: any bad entry -> 422 for the whole request, nothing changed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "counts",
    [
        {"2026-09-29": 60, "2026-13-01": 5},  # bad date
        {"2026-09-29": 60, "09/30/2026": 5},  # wrong date format
        {"2026-09-29": 60, "2026-09-30": -1},  # negative
        {"2026-09-29": 60, "2026-09-30": "57"},  # string, not int
        {"2026-09-29": 60, "2026-09-30": 5.5},  # float
        {"2026-09-29": 60, "2026-09-30": True},  # bool is not a count
        {"2026-09-29": 60, "2026-09-30": None},
    ],
)
def test_invalid_entry_rejects_whole_request_with_422(unauth_client, counts):
    _post(unauth_client, {"2026-09-29": 10})
    before = _snapshot(unauth_client)

    resp = _post(unauth_client, counts)
    assert resp.status_code == 422
    assert resp.json()["error"]["field"] == "counts"
    assert _snapshot(unauth_client) == before


@pytest.mark.parametrize("body", [{}, {"counts": []}, {"counts": "x"}, [1, 2]])
def test_malformed_body_is_422(unauth_client, body):
    resp = unauth_client.post(INGEST_PATH, json=body, headers={"X-Relay-Key": TEST_RELAY_KEY})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# End to end: the fed value reaches the production brief and the form
# ---------------------------------------------------------------------------


def test_brief_summary_reflects_the_fed_value(unauth_client):
    # 2026-09-29 is a Tuesday - "yesterday" as of Wednesday 2026-09-30.
    _post(unauth_client, {"2026-09-29": 171})
    resp = unauth_client.get(
        "/api/v1/brief/summary",
        params={"product": "drawers", "asof": "2026-09-30"},
        headers={"X-Brief-Key": TEST_BRIEF_KEY},
    )
    assert resp.status_code == 200
    last_day = resp.json()["last_production_day"]
    assert last_day["date"] == "2026-09-29"
    assert last_day["entered"] is True
    assert last_day["inspected"] == 171


def test_manual_save_after_feed_keeps_fed_value_and_rejects_typing_it(unauth_client):
    _post(unauth_client, {"2026-09-29": 171})
    _login(unauth_client)

    rejected = unauth_client.put(
        "/api/v1/daily-production/2026-09-29",
        json={"shift": "Day", "drawers_inspected": 5, "drawers_rejected_unique": 2},
    )
    assert rejected.status_code == 422

    ok = unauth_client.put(
        "/api/v1/daily-production/2026-09-29",
        json={"shift": "Day", "drawers_rejected_unique": 2},
    )
    assert ok.status_code == 200
    assert ok.json()["drawers_inspected"] == 171
    assert ok.json()["drawers_rejected_unique"] == 2


def test_daily_summary_form_shows_inspected_read_only(unauth_client):
    _login(unauth_client)
    html = unauth_client.get("/daily-summary").text
    start = html.index('id="drawers_inspected"')
    tag = html[html.rindex("<input", 0, start) : html.index(">", start)]
    assert "readonly" in tag
    # No name= attribute: FormData can't pick it up, so the form never sends it.
    assert "name=" not in tag
    assert "from Production Count" in html
    assert "drawers_inspected: Number(" not in html
