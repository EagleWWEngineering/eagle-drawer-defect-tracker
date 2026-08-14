"""API test fixtures: a TestClient wired to an isolated in-memory SQLite DB."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.dependencies import get_db
from app.main import app
from app.seed_data import seed_master_data
from app.services import auth_service


@pytest.fixture()
def client():
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
    # Intentionally NOT using `with TestClient(app) as ...`: that would run app.main's
    # startup event, which seeds master data into the REAL data/defect_tracker.db via
    # app.database.SessionLocal (dependency_overrides only affects routes, not
    # lifespan events). Tests must stay isolated from the real database file.
    test_client = TestClient(app)
    test_client.testing_sessionmaker = TestingSession  # lets tests inspect DB rows directly

    # This fixture is used by ~everything except the dedicated auth tests, which
    # care about the login flow itself - so pre-authenticate here by creating a
    # session row directly (bypassing the login form/credentials entirely) and
    # attaching its cookie, the same way a browser would after a real login.
    auth_db = TestingSession()
    token = auth_service.create_session(auth_db)
    auth_db.close()
    test_client.cookies.set(auth_service.SESSION_COOKIE_NAME, token)

    try:
        yield test_client
    finally:
        test_client.close()
        app.dependency_overrides.clear()


@pytest.fixture()
def master_data(client):
    resp = client.get("/api/v1/master-data")
    assert resp.status_code == 200
    data = resp.json()
    stations = {s["name"]: s["id"] for s in data["stations"]}
    categories = {c["name"]: c["id"] for c in data["defect_categories"]}
    return {"stations": stations, "categories": categories}


@pytest.fixture()
def customer_categories(client):
    resp = client.get("/api/v1/customer-issues/categories")
    assert resp.status_code == 200
    return {c["name"]: c["id"] for c in resp.json()}
