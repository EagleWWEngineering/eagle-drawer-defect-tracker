"""FastAPI application entry point.

Run with: uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.config import get_settings
from app.database import SessionLocal
from app.dependencies import get_db
from app.errors import InvalidTransitionError, NotFoundError, ServiceError, ValidationError
from app.routers import (
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
from app.services import sync_service

settings = get_settings()
APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


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

    # Phase 3: pull Customer Issues from the production brief on startup, then every
    # SYNC_INTERVAL_MINUTES. Cancelled cleanly on shutdown. run_sync() never raises
    # (see sync_service.py), so an unreachable production brief just gets logged and
    # retried on the next interval - it never prevents the app from serving requests.
    sync_task = asyncio.create_task(sync_service.run_periodic_sync(settings.sync_interval_minutes))
    try:
        yield
    finally:
        sync_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sync_task


app = FastAPI(
    title="Eagle Drawer Defect Tracker API",
    version="0.1.0",
    description="Local drawer-production defect tracking API. See docs/PROJECT_SPEC.md.",
    lifespan=lifespan,
)

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
