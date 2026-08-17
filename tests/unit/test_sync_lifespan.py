"""Verifies app.main's lifespan no longer schedules an automatic periodic sync
background task on startup.

Historical note: this used to assert the opposite (that lifespan DID schedule
sync_service.run_periodic_sync() and cancelled it cleanly on shutdown). That task
was retired because Render's servers cannot reach the production brief directly
(confirmed firewalled) - every automatic run always failed there. Customer Issues
sync now happens exclusively via a local relay (scripts/relay_customer_issues.py,
scripts/relay_poll.py) POSTing to POST /api/v1/sync/customer-issues/ingest-raw. See
app/main.py's lifespan docstring/comment and app/services/sync_service.py's module
docstring for the full picture. sync_service.run_periodic_sync()/run_sync() are
still real, callable functions - just never invoked automatically anymore.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.database import Base
from app.services import sync_service


def _make_test_sessionmaker():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


async def test_lifespan_does_not_schedule_periodic_sync(monkeypatch):
    test_sessionmaker = _make_test_sessionmaker()
    monkeypatch.setattr(main_module, "SessionLocal", test_sessionmaker)

    calls = []

    async def fake_run_periodic_sync(interval_minutes, session_factory=None):
        calls.append(interval_minutes)

    monkeypatch.setattr(sync_service, "run_periodic_sync", fake_run_periodic_sync)

    async with main_module.lifespan(main_module.app):
        # Give any stray scheduled task a tick to actually start, if one existed.
        await asyncio.sleep(0.05)

    # No background task was ever created, so run_periodic_sync is never called.
    assert calls == []


async def test_lifespan_still_seeds_master_data(monkeypatch):
    """Sanity check that retiring the sync task didn't accidentally break the other
    startup responsibility lifespan still has."""
    test_sessionmaker = _make_test_sessionmaker()
    monkeypatch.setattr(main_module, "SessionLocal", test_sessionmaker)

    async with main_module.lifespan(main_module.app):
        db = test_sessionmaker()
        try:
            from app.models import Station

            assert db.query(Station).count() > 0
        finally:
            db.close()
