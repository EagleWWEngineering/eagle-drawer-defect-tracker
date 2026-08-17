"""Production brief sync control (Phase 3): trigger a sync, check its status.

Also hosts the relay ingest endpoint (POST .../ingest-raw): Render's servers cannot
reach the production brief directly (firewalled on the production brief's side,
confirmed by a connection timeout from Render's own shell), so a local relay script
(scripts/relay_customer_issues.py), running on a machine that CAN reach it, fetches
the raw data and forwards it here instead.

Follow-up (also in this file): the Customer Issues tab's "Sync Now" button used to
call POST /customer-issues directly, which made Render attempt (and always fail) a
direct fetch. It now calls POST .../request-manual-sync instead, which just records
a pending flag and returns instantly - see app/services/sync_service.py's module
docstring. GET .../relay-status is the local relay's frequent (~1 minute) heartbeat
that notices that flag and triggers a real relay pass. GET .../relay-connection is
what the UI itself polls to render its connected/disconnected status line.

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
from app.schemas import (
    ManualSyncRequestOut,
    RelayConnectionStatusOut,
    RelayHeartbeatOut,
    SyncLogOut,
)
from app.services import sync_service

router = APIRouter(prefix="/api/v1/sync", tags=["sync"])


@router.post("/customer-issues", response_model=SyncLogOut)
async def trigger_sync(db: Session = Depends(get_db)) -> SyncLogOut:
    """Manual debug route: run one direct-fetch sync immediately and return its
    outcome. Kept working for local-network use/debugging, but the Customer Issues
    tab's "Sync Now" button no longer calls this - see request_manual_sync below."""
    log = await sync_service.run_sync(db)
    return SyncLogOut.model_validate(log)


@router.post("/customer-issues/request-manual-sync", response_model=ManualSyncRequestOut)
def request_manual_sync(db: Session = Depends(get_db)) -> ManualSyncRequestOut:
    """ "Sync Now" button on the Customer Issues tab. Behind the normal login
    session (no special exemption) - a logged-in browser calls this. Records that a
    manual sync is wanted and returns immediately; no network call is made here, so
    this always succeeds instantly. The actual fetch+ingest happens later, on the
    local relay's next frequent check-in (see relay_status below)."""
    requested_at = sync_service.request_manual_sync(db)
    return ManualSyncRequestOut(
        message="Sync requested — will run shortly if your local relay is online.",
        requested_at=requested_at,
    )


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


@router.get("/customer-issues/relay-status", response_model=RelayHeartbeatOut)
def relay_status(
    x_relay_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> RelayHeartbeatOut:
    """Heartbeat called by the local relay's frequent (~1 minute) check-in
    (scripts/relay_poll.py). Authenticated the same way as ingest-raw (RELAY_API_KEY
    header) since this is also an unattended machine-to-machine caller that can't
    hold a browser session - see _verify_relay_key and app/auth_middleware.py
    PUBLIC_EXACT_PATHS, which exempts this one exact path too.

    Cheap and fast: no network call to the production brief happens here, just a DB
    read+write. The call itself IS the heartbeat (updates sync_relay_last_seen_at to
    now); the response tells the relay whether a manual sync is pending so it knows
    whether to also run a full fetch+ingest this cycle.
    """
    _verify_relay_key(x_relay_key)
    pending = sync_service.record_relay_heartbeat(db)
    return RelayHeartbeatOut(manual_sync_pending=pending)


@router.get("/customer-issues/relay-connection", response_model=RelayConnectionStatusOut)
def relay_connection(db: Session = Depends(get_db)) -> RelayConnectionStatusOut:
    """What the Customer Issues tab polls to render its 🟢/🔴 connected status line.
    Behind the normal login session, like every other UI-facing endpoint - this is
    read-only and has no heartbeat side effect itself (only GET relay-status above,
    called by the relay script, updates the heartbeat)."""
    status = sync_service.relay_connection_status(db)
    return RelayConnectionStatusOut(**status)


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
