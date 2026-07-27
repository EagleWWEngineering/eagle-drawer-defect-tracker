"""Shared pytest fixtures: an isolated in-memory SQLite DB per test, seeded with
baseline master data, plus a FastAPI TestClient wired to that same DB."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import CustomerIssueCategory, DefectCategory, Station
from app.seed_data import seed_master_data


@pytest.fixture()
def db_session():
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
    session: Session = TestingSession()
    seed_master_data(session)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def stations(db_session):
    return {s.name: s for s in db_session.query(Station).all()}


@pytest.fixture()
def categories(db_session):
    return {c.name: c for c in db_session.query(DefectCategory).all()}


@pytest.fixture()
def customer_categories(db_session):
    return {c.name: c for c in db_session.query(CustomerIssueCategory).all()}


@pytest.fixture()
def today() -> dt.date:
    return dt.date(2026, 7, 24)
