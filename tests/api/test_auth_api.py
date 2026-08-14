"""Phase 2 auth tests: single shared login, non-expiring sessions, logout, and
"log out everywhere". Uses its own unauthenticated `raw_client` fixture (unlike the
shared `client` fixture in conftest.py, which pre-authenticates for every other test
file so ~everything else can ignore auth entirely)."""

from __future__ import annotations

import datetime as dt

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.dependencies import get_db
from app.main import app
from app.models import AuthSession
from app.seed_data import seed_master_data
from app.services import auth_service

TEST_USERNAME = "testuser"
TEST_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def raw_client(monkeypatch):
    monkeypatch.setenv("APP_USERNAME", TEST_USERNAME)
    monkeypatch.setenv(
        "APP_PASSWORD_HASH",
        bcrypt.hashpw(TEST_PASSWORD.encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
    )

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


def _login(raw_client, username=TEST_USERNAME, password=TEST_PASSWORD):
    return raw_client.post("/api/v1/auth/login", json={"username": username, "password": password})


# ---------------------------------------------------------------------------
# Login required on (almost) every route
# ---------------------------------------------------------------------------


def test_health_check_does_not_require_login(raw_client):
    resp = raw_client.get("/api/v1/health")
    assert resp.status_code == 200


def test_static_assets_reachable_without_login(raw_client):
    resp = raw_client.get("/static/css/app.css")
    assert resp.status_code == 200


def test_login_page_itself_is_reachable_without_a_session(raw_client):
    resp = raw_client.get("/login")
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/reports/summary",
        "/api/v1/defect-cases",
        "/api/v1/daily-production",
        "/api/v1/master-data",
        "/api/v1/customer-issues",
    ],
)
def test_protected_api_routes_require_login(raw_client, path):
    resp = raw_client.get(path)
    assert resp.status_code == 401
    assert resp.json()["error"]["message"]


@pytest.mark.parametrize(
    "path",
    ["/", "/defect-entry", "/daily-summary", "/rework-queue", "/reports", "/admin", "/settings"],
)
def test_protected_pages_redirect_to_login(raw_client, path):
    resp = raw_client.get(path, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_uploads_are_not_publicly_reachable(raw_client):
    """Photos are real shop data, unlike /static - they must not be exempted."""
    resp = raw_client.get("/uploads/does-not-exist.jpg", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


# ---------------------------------------------------------------------------
# Correct / incorrect credentials
# ---------------------------------------------------------------------------


def test_login_with_correct_credentials_grants_access(raw_client):
    resp = _login(raw_client)
    assert resp.status_code == 200
    assert auth_service.SESSION_COOKIE_NAME in resp.cookies

    followup = raw_client.get("/api/v1/reports/summary")
    assert followup.status_code == 200


def test_login_with_wrong_password_is_rejected(raw_client):
    resp = _login(raw_client, password="not-the-password")
    assert resp.status_code == 400
    assert auth_service.SESSION_COOKIE_NAME not in resp.cookies

    followup = raw_client.get("/api/v1/reports/summary")
    assert followup.status_code == 401


def test_login_with_wrong_username_is_rejected(raw_client):
    resp = _login(raw_client, username="not-the-user")
    assert resp.status_code == 400


def test_login_with_missing_credentials_returns_422(raw_client):
    resp = raw_client.post("/api/v1/auth/login", json={"username": TEST_USERNAME})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Sessions never expire based on time
# ---------------------------------------------------------------------------


def test_a_very_old_session_row_is_still_accepted(raw_client):
    """No code path checks session age - a session is valid purely because its row
    still exists (app/services/auth_service.py)."""
    db = raw_client.testing_sessionmaker()
    token = auth_service.create_session(db)
    row = db.query(AuthSession).filter(AuthSession.token == token).first()
    row.created_at = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    db.commit()
    db.close()

    raw_client.cookies.set(auth_service.SESSION_COOKIE_NAME, token)
    resp = raw_client.get("/api/v1/reports/summary")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Normal logout: only this device/session
# ---------------------------------------------------------------------------


def test_logout_ends_only_the_current_session(raw_client):
    _login(raw_client)

    # A second "device" - its own session row, never sent through this client.
    db = raw_client.testing_sessionmaker()
    other_token = auth_service.create_session(db)
    db.close()

    logout_resp = raw_client.post("/api/v1/auth/logout")
    assert logout_resp.status_code == 200

    # This device is now logged out.
    resp = raw_client.get("/api/v1/reports/summary")
    assert resp.status_code == 401

    # The other device's session is completely untouched.
    db = raw_client.testing_sessionmaker()
    assert auth_service.get_session(db, other_token) is not None
    db.close()


# ---------------------------------------------------------------------------
# "Log out everywhere": all sessions, requires re-entering the password
# ---------------------------------------------------------------------------


def test_logout_everywhere_requires_correct_password(raw_client):
    _login(raw_client)
    db = raw_client.testing_sessionmaker()
    other_token = auth_service.create_session(db)
    db.close()

    resp = raw_client.post("/api/v1/auth/logout-everywhere", json={"password": "wrong"})
    assert resp.status_code == 400

    # Nothing was invalidated by the failed attempt.
    db = raw_client.testing_sessionmaker()
    assert auth_service.get_session(db, other_token) is not None
    db.close()
    still_ok = raw_client.get("/api/v1/reports/summary")
    assert still_ok.status_code == 200


def test_logout_everywhere_invalidates_every_session(raw_client):
    _login(raw_client)
    db = raw_client.testing_sessionmaker()
    other_token = auth_service.create_session(db)
    db.close()

    resp = raw_client.post("/api/v1/auth/logout-everywhere", json={"password": TEST_PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["sessions_invalidated"] >= 2

    db = raw_client.testing_sessionmaker()
    assert auth_service.get_session(db, other_token) is None
    db.close()

    # The device that triggered "log out everywhere" is also logged out and must
    # re-authenticate, same as every other device.
    resp2 = raw_client.get("/api/v1/reports/summary")
    assert resp2.status_code == 401
