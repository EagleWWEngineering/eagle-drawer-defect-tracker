"""MCP test fixtures.

The MCP server's httpx client is pointed at the FastAPI app in-process via
httpx.ASGITransport - no real TCP port or running uvicorn process is needed, and the
app's DB dependency is overridden the same way tests/api/conftest.py does it, so MCP
tests never touch the real data/defect_tracker.db file.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.dependencies import get_db
from app.main import app
from app.seed_data import seed_master_data
from app.services import auth_service
from mcp_server import server as mcp_module


@pytest.fixture()
def mcp_env(monkeypatch):
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

    # The MCP server calls the same REST API the UI uses (CLAUDE.md architecture
    # rule), so it's subject to the same Phase 2 login requirement. Pre-authenticate
    # by creating a session row directly and attaching its cookie to the httpx
    # client, the same way a real MCP server process logs in once via
    # POST /api/v1/auth/login and then reuses that session indefinitely.
    auth_db = TestingSession()
    token = auth_service.create_session(auth_db)
    auth_db.close()

    test_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={auth_service.SESSION_COOKIE_NAME: token},
    )
    monkeypatch.setattr(mcp_module, "_client", test_client)
    monkeypatch.setattr(mcp_module, "_master_data_cache", None)

    yield mcp_module, TestingSession

    app.dependency_overrides.clear()
