"""FastAPI application entry point.

Run with: uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.auth_middleware import LoginRequiredMiddleware
from app.config import get_settings
from app.database import SessionLocal
from app.dependencies import get_db
from app.errors import InvalidTransitionError, NotFoundError, ServiceError, ValidationError
from app.routers import (
    admin,
    auth,
    customer_issues,
    daily_production,
    defect_cases,
    exports,
    master_data,
    reports,
    sync,
)
from app.routers import settings as settings_router
from app.schemas import HealthOut
from app.seed_data import seed_master_data

settings = get_settings()
APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

logger = logging.getLogger("app.startup")


def _warn_if_env_var_missing_for_persistent_path(env_var_name: str, resolved_path: object) -> None:
    """Incident follow-up: a 2026-08-17 photo-upload 404 traced back to UPLOADS_DIR
    being declared in render.yaml with a plain default value, but never actually
    entered in Render's dashboard for the already-existing service - Render does
    not retroactively apply a newly-added render.yaml env var to a service that
    wasn't just (re)created via its Blueprint flow. The app silently fell back to
    a path that doesn't survive a redeploy, with nothing in the logs to say so.
    This makes that specific failure mode loud instead of silent, for this and any
    future persistent-disk-dependent setting (see DATABASE_URL below too)."""
    if not os.getenv(env_var_name):
        logger.warning(
            "%s is not set in the environment - falling back to %s, which is only "
            "safe if this path happens to already sit on a persistent disk. If "
            "photos/data uploaded here need to survive a redeploy, set %s "
            "explicitly (see render.yaml) and confirm it's actually present in "
            "the host's dashboard, not just declared in render.yaml.",
            env_var_name,
            resolved_path,
            env_var_name,
        )


_warn_if_env_var_missing_for_persistent_path("DATABASE_URL", settings.database_url)
_warn_if_env_var_missing_for_persistent_path("UPLOADS_DIR", settings.uploads_dir)


def static_version(relative_path: str) -> int:
    """File mtime as a cache-busting query param, so an updated static/js/*.js
    file is never served stale from a browser's cache after a code change."""
    return int((APP_DIR / "static" / relative_path).stat().st_mtime)


templates.env.globals["static_version"] = static_version


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db = SessionLocal()
    try:
        seed_master_data(db)
    finally:
        db.close()

    # Phase 3 originally scheduled sync_service.run_periodic_sync() here to pull
    # Customer Issues from the production brief every SYNC_INTERVAL_MINUTES.
    # Retired: Render's servers cannot reach the production brief directly
    # (confirmed firewalled), so every one of those automatic attempts always
    # failed. Customer Issues sync now happens exclusively via a local relay that
    # CAN reach the production brief - scripts/relay_customer_issues.py (the
    # existing unconditional hourly fetch+ingest) and scripts/relay_poll.py (a new
    # ~1-minute heartbeat that also runs a full relay pass when a "Sync Now" click
    # has requested one) - both POSTing to
    # POST /api/v1/sync/customer-issues/ingest-raw. sync_service.run_periodic_sync()
    # / run_sync() and the manual POST /api/v1/sync/customer-issues debug route are
    # deliberately left in place (not deleted) for a future local-network
    # deployment or manual debugging - only this automatic background task is
    # retired, so it no longer fires on its own.
    yield


app = FastAPI(
    title="Eagle Drawer Defect Tracker API",
    version="0.1.0",
    description="Local drawer-production defect tracking API. See docs/PROJECT_SPEC.md.",
    lifespan=lifespan,
)

app.add_middleware(LoginRequiredMiddleware)

app.include_router(auth.router)
app.include_router(defect_cases.router)
app.include_router(daily_production.router)
app.include_router(reports.router)
app.include_router(reports.rework_router)
app.include_router(master_data.router)
app.include_router(exports.router)
app.include_router(customer_issues.router)
app.include_router(customer_issues.export_router)
app.include_router(sync.router)
app.include_router(settings_router.router)
# TEMPORARY - see app/routers/admin.py docstring for the removal plan.
app.include_router(admin.router)

app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
settings.uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(settings.uploads_dir)), name="uploads")


# ---------------------------------------------------------------------------
# HTML page routes (server-rendered shell; all data comes from the JSON API above)
# ---------------------------------------------------------------------------


@app.get("/")
def page_dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/defect-entry")
def page_defect_entry(request: Request):
    return templates.TemplateResponse(request, "defect_entry.html")


@app.get("/daily-summary")
def page_daily_summary(request: Request):
    return templates.TemplateResponse(request, "daily_summary.html")


@app.get("/rework-queue")
def page_rework_queue(request: Request):
    return templates.TemplateResponse(request, "rework_queue.html")


@app.get("/reports")
def page_reports(request: Request):
    return templates.TemplateResponse(request, "reports.html")


@app.get("/customer-issues")
def page_customer_issues(request: Request):
    return templates.TemplateResponse(request, "customer_issues.html")


@app.get("/admin")
def page_admin(request: Request):
    return templates.TemplateResponse(request, "admin.html")


@app.get("/print-daily-log")
def page_print_daily_log(request: Request):
    return templates.TemplateResponse(request, "print_daily_log.html")


@app.get("/settings")
def page_settings(request: Request):
    """Behind the login like every other page (LoginRequiredMiddleware) - holds the
    "Log out" / "Log out everywhere" actions (Phase 2)."""
    return templates.TemplateResponse(request, "settings.html")


@app.get("/login")
def page_login(request: Request):
    """Public (see app/auth_middleware.py PUBLIC_EXACT_PATHS) - the one page
    reachable with no session at all, other than the health check."""
    return templates.TemplateResponse(request, "login.html")


def _error_status_code(exc: ServiceError) -> int:
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, (ValidationError, InvalidTransitionError)):
        return 400
    return 400


@app.exception_handler(ServiceError)
async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
    """Every business-rule failure returns this same shape — never a raw traceback."""
    return JSONResponse(
        status_code=_error_status_code(exc),
        content={"error": {"message": exc.message, "field": exc.field}},
    )


@app.get("/api/v1/health", response_model=HealthOut)
def health(db=Depends(get_db)) -> HealthOut:
    # Uses the injected session (not the raw engine) so tests that override get_db
    # never touch the real data/defect_tracker.db file.
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:  # noqa: BLE001 - health check must never raise, just report status
        db_status = "unavailable"
    return HealthOut(status="ok", database=db_status)
