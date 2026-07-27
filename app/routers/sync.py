"""Production brief sync control (Phase 3): trigger a sync, check its status.

HTTP input/output only - all sync logic lives in app/services/sync_service.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.models import SyncLog
from app.schemas import SyncLogOut
from app.services import sync_service

router = APIRouter(prefix="/api/v1/sync", tags=["sync"])


@router.post("/customer-issues", response_model=SyncLogOut)
async def trigger_sync(db: Session = Depends(get_db)) -> SyncLogOut:
    """ "Sync Now" button: run one sync immediately and return its outcome."""
    log = await sync_service.run_sync(db)
    return SyncLogOut.model_validate(log)


@router.get("/status", response_model=SyncLogOut | None)
def get_sync_status(db: Session = Depends(get_db)) -> SyncLogOut | None:
    """The single most recent sync attempt, for the Customer Issues tab's status
    line. Returns null if a sync has never run."""
    log = db.query(SyncLog).order_by(SyncLog.sync_started_at.desc()).first()
    return SyncLogOut.model_validate(log) if log else None


@router.get("/logs", response_model=list[SyncLogOut])
def list_sync_logs(db: Session = Depends(get_db), limit: int = 20) -> list[SyncLogOut]:
    """The last `limit` sync attempts, for the Admin screen's Sync Log section."""
    logs = db.query(SyncLog).order_by(SyncLog.sync_started_at.desc()).limit(limit).all()
    return [SyncLogOut.model_validate(log) for log in logs]
