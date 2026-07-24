"""FastAPI application entry point.

Run with: uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import get_settings
from app.database import SessionLocal, engine
from app.errors import InvalidTransitionError, NotFoundError, ServiceError, ValidationError
from app.routers import daily_production, defect_cases, exports, master_data, reports
from app.schemas import HealthOut
from app.seed_data import seed_master_data

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db = SessionLocal()
    try:
        seed_master_data(db)
    finally:
        db.close()
    yield


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

app.mount("/static", StaticFiles(directory="app/static"), name="static")


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
def health() -> HealthOut:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:  # noqa: BLE001 - health check must never raise, just report status
        db_status = "unavailable"
    return HealthOut(status="ok", database=db_status)
