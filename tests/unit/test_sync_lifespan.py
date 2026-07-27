"""Verifies app.main's lifespan schedules the periodic sync background task on
startup and cancels it cleanly on shutdown - without touching the real database or
making any real network call.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.database import Base


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


async def test_lifespan_schedules_and_cancels_periodic_sync(monkeypatch):
    test_sessionmaker = _make_test_sessionmaker()
    monkeypatch.setattr(main_module, "SessionLocal", test_sessionmaker)

    calls = []

    async def fake_run_periodic_sync(interval_minutes, session_factory=None):
        calls.append(interval_minutes)
        try:
            # Simulate the real function's infinite loop so we can prove
            # cancellation actually reaches it, without ever really looping.
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            calls.append("cancelled")
            raise

    monkeypatch.setattr(main_module.sync_service, "run_periodic_sync", fake_run_periodic_sync)

    async with main_module.lifespan(main_module.app):
        # Give the scheduled task a tick to actually start running.
        await asyncio.sleep(0.05)
        assert calls == [main_module.settings.sync_interval_minutes]

    # On exit, lifespan cancels the task and awaits it - proving shutdown is clean.
    assert calls[-1] == "cancelled"
