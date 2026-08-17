"""Production brief sync control (Phase 3): trigger a sync, check its status.

Also hosts the relay ingest endpoint (POST .../ingest-raw): Render's servers cannot
reach the production brief directly (firewalled on the production brief's side,
confirmed by a connection timeout from Render's own shell), so a local relay script
(scripts/relay_customer_issues.py), running on a machine that CAN reach it, fetches
the raw data and forwards it here instead.

HTTP input/output only - all sync logic lives in app/services/sync_service.py.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dependencies import get_db
from app.errors import ValidationError
from app.models import SyncLog
from app.schemas import SyncLogOut
from app.services import sync_service

router = APIRouter(prefix="/api/v1/sync", tags=["sync"])


@router.post("/customer-issues", response_model=SyncLogOut)
async def trigger_sync(db: Session = Depends(get_db)) -> SyncLogOut:
    """ "Sync Now" button: run one sync immediately and return its outcome."""
    log = await sync_service.run_sync(db)
    return SyncLogOut.model_validate(log)


def _verify_relay_key(x_relay_key: str | None) -> None:
    """Constant-time comparison against RELAY_API_KEY - a secret deliberately
    separate from the human shared login (APP_USERNAME/APP_PASSWORD_HASH), same
    discipline as app/services/auth_service.py verify_credentials. Missing/wrong
    key -> 401, same security bar as being behind the human login, just a
    different credential appropriate for an unattended, machine-to-machine caller
    (scripts/relay_customer_issues.py) that can't hold a browser session - which is
    exactly why this one exact path is exempted from LoginRequiredMiddleware (see
    app/auth_middleware.py PUBLIC_EXACT_PATHS)."""
    expected = get_settings().relay_api_key
    if not expected or not x_relay_key or not secrets.compare_digest(x_relay_key, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid relay key.")


@router.post("/customer-issues/ingest-raw", response_model=SyncLogOut)
async def ingest_raw_customer_issues(
    payload: dict[str, Any] = Body(...),
    x_relay_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> SyncLogOut:
    """Receive the exact raw JSON body the production brief's /api/quality-issues
    returns (the same shape sync_service.fetch_issues() returns) - already fetched
    by the local relay script from a machine that can reach the production brief.

    Skips fetch_issues() entirely and calls sync_service.process_issues_payload()
    directly, so every bit of field-mapping/dedup/category-matching logic stays
    identical to (and shared with) the hourly direct-fetch sync. The resulting
    SyncLog.source_url is prefixed "relay:" to distinguish it from a direct-fetch
    sync in Admin's sync history.
    """
    _verify_relay_key(x_relay_key)

    try:
        sync_service.validate_raw_payload(payload)
    except sync_service.ProductionBriefError as exc:
        raise ValidationError(str(exc)) from exc

    settings = get_settings()
    source_url = f"relay:{settings.production_brief_url}{sync_service.QUALITY_ISSUES_PATH}"
    log = sync_service.process_issues_payload(db, payload, source_url=source_url)
    return SyncLogOut.model_validate(log)


@router.get("/status", response_model=SyncLogOut | None)
def get_sync_status(db: Session = Depends(get_db)) -> SyncLogOut | None:
    """The single most recent sync attempt, for the Customer Issues tab's status
    line. Returns null if a sync has never run."""
    log = db.query(SyncLog).order_by(SyncLog.sync_started_at.desc(), SyncLog.id.desc()).first()
    return SyncLogOut.model_validate(log) if log else None


@router.get("/logs", response_model=list[SyncLogOut])
def list_sync_logs(db: Session = Depends(get_db), limit: int = 20) -> list[SyncLogOut]:
    """The last `limit` sync attempts, for the Admin screen's Sync Log section.

    Ordered by (sync_started_at, id) both descending - id is the tiebreaker for
    successive syncs whose wall-clock timestamps tie (real risk: two "Sync Now"
    clicks in quick succession, or a fast test loop).
    """
    logs = (
        db.query(SyncLog)
        .order_by(SyncLog.sync_started_at.desc(), SyncLog.id.desc())
        .limit(limit)
        .all()
    )
    return [SyncLogOut.model_validate(log) for log in logs]
