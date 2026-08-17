"""Sync Customer Issues from the Eagle production brief's JSON API (Phase 3).

See docs/PROJECT_SPEC_PHASE3.md. This is the only place that talks to the
production brief - the REST API, UI, and MCP server never call it directly, and the
production brief never touches our database directly either. Deduplication key is
CustomerIssue.source_thread_id (the brief's thread_id).

CRITICAL: sync failures must never crash the app. run_sync() always returns a
SyncLog (status "success" or "failed") instead of raising, so both the hourly
background task and the "Sync Now" API route can treat every outcome uniformly.

Two ways raw production-brief data reaches process_issues_payload() (the shared
mapping/dedup/upsert logic):
  1. run_sync() - fetch_issues() pulls it directly over HTTP. Render's servers
     cannot reach the production brief directly (firewalled on the production
     brief's side), so this path always fails there - which is why app.main's
     lifespan no longer schedules run_periodic_sync() automatically. run_sync()
     and the manual "Sync Now" debug route (POST /api/v1/sync/customer-issues)
     are kept for local-network/debugging use.
  2. POST /api/v1/sync/customer-issues/ingest-raw (app/routers/sync.py) - a local
     relay script (scripts/relay_customer_issues.py), running on a machine that CAN
     reach the production brief, fetches the same data and forwards the raw JSON
     body here. This path skips fetch_issues() entirely but calls the exact same
     process_issues_payload(), so mapping/dedup/category logic is never duplicated.
     SyncLog rows from this path are distinguished by a "relay:" prefix on
     source_url. This is the ONLY path that actually runs in production now.

The "Sync Now" button on the Customer Issues tab no longer calls run_sync()
directly (Render can't reach the production brief). Instead it calls POST
/api/v1/sync/customer-issues/request-manual-sync, which just sets
MANUAL_SYNC_REQUESTED_SETTING_KEY and returns instantly. A second local script
(scripts/relay_poll.py), polling GET /api/v1/sync/customer-issues/relay-status
about once a minute, notices the pending flag and performs the real fetch+ingest
via the exact same code path as #2 above. See the AppSetting keys/helpers below.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import logging
import re
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import AppSetting, CustomerIssue, CustomerIssueCategory, SyncLog
from app.services import customer_issue_service

logger = logging.getLogger("sync_service")

QUALITY_ISSUES_PATH = "/api/quality-issues"

# Singleton state (see app/models.py AppSetting docstring - "intentionally generic
# so a future setting doesn't need its own table/migration"), following the exact
# pattern app/services/auth_service.py already uses for AUTH_USERNAME_SETTING_KEY
# etc. Both are stored as ISO datetime strings; empty/missing means "never".
#
# Background: Render cannot reach the production brief directly (confirmed
# firewalled), so the "Sync Now" button no longer triggers a direct fetch from
# Render (that always failed there). Instead it just records that a sync is
# wanted (MANUAL_SYNC_REQUESTED_SETTING_KEY) and returns instantly; the local
# relay (scripts/relay_poll.py), polling every ~1 minute via GET
# /api/v1/sync/customer-issues/relay-status, notices the pending flag and performs
# the real fetch+ingest. That same endpoint call is also the heartbeat the
# Customer Issues tab's connected/disconnected status line is computed from
# (RELAY_LAST_SEEN_SETTING_KEY).
MANUAL_SYNC_REQUESTED_SETTING_KEY = "sync_manual_requested_at"
RELAY_LAST_SEEN_SETTING_KEY = "sync_relay_last_seen_at"

# How stale the relay's last heartbeat can be before the UI reports it as
# disconnected. The relay's frequent check-in polls roughly every minute (see
# scripts/relay_poll.py) - 2 minutes allows exactly one missed cycle before
# flipping the status line red.
RELAY_HEARTBEAT_STALE_AFTER = dt.timedelta(minutes=2)

# The brief's classifier can take up to ~1 day to finish processing an email and
# add it to /api/quality-issues, tagged with the day it was *received* rather
# than the day it was classified. Advancing `since` to the exact date of the
# last successful sync (even one that fetched 0 records) can permanently skip
# such late-classified issues once "since" passes their day. This buffer makes
# every incremental sync re-check a trailing window; re-synced issues are
# idempotent (upserted by source_thread_id), so the overlap is harmless.
SINCE_LOOKBACK_BUFFER_DAYS = 3

# Spec-defined subcategory -> CustomerIssueCategory.name mapping. Anything not
# listed here falls back to "Other" (see _map_category).
SUBCATEGORY_TO_CATEGORY_NAME: dict[str, str] = {
    "wrong size": "Wrong Size",
    "wrong spec": "Wrong Spec",
    "joinery": "Joinery",
    "out of square": "Joinery",
    "finish quality": "Finish Quality",
    "finish": "Finish Quality",
    "missing parts": "Missing Parts",
    "crushed box": "Shipping Damage / Crushed Box",
    "shipping damage": "Shipping Damage / Crushed Box",
    "corner impact": "Corner Impact",
    "warp or crack": "Warp or Crack",
    "warp": "Warp or Crack",
    "crack": "Warp or Crack",
    "hinge": "Hinge Holes",
    "hinge holes": "Hinge Holes",
}

# Spec-defined production-brief "category" -> CustomerIssue.source_type. Anything
# unrecognized defaults to "Manufacturing" (the more common source type in practice).
BRIEF_CATEGORY_TO_SOURCE_TYPE: dict[str, str] = {
    "manufacturing": "Manufacturing",
    "shipping-damage": "Shipping Damage",
}

NEEDS_REVIEW_NOTE = "⚠ Low confidence classification — needs review."

_PIECE_COUNT_RE = re.compile(r"(\d+)\s*pc", re.IGNORECASE)


class ProductionBriefError(RuntimeError):
    """Raised for any failure calling/parsing the production brief's API."""


def _build_client() -> httpx.AsyncClient:
    """Separate factory so tests can monkeypatch this to inject a MockTransport
    instead of hitting the real network (see tests/unit/test_sync_service.py)."""
    settings = get_settings()
    return httpx.AsyncClient(base_url=settings.production_brief_url, timeout=30.0)


def validate_raw_payload(data: Any) -> dict[str, Any]:
    """Validate that `data` has the shape the rest of this module expects: a dict
    with an "issues" list. Shared by fetch_issues() (validating a freshly-fetched
    HTTP response body from the production brief) and the relay ingest endpoint
    (POST /api/v1/sync/customer-issues/ingest-raw, app/routers/sync.py), which
    receives this exact same shape as a request body - already fetched by the local
    relay script (scripts/relay_customer_issues.py) from a machine that can reach
    the production brief.

    Raises ProductionBriefError on any shape mismatch. The ingest endpoint catches
    this and turns it into a clean 400, since a malformed *request body* is a caller
    error, not a "the production brief is unreachable" sync failure.
    """
    if not isinstance(data, dict) or "issues" not in data:
        raise ProductionBriefError("Payload is missing the expected 'issues' field.")
    if not isinstance(data["issues"], list):
        raise ProductionBriefError("Payload's 'issues' field must be a list.")
    return data


async def fetch_issues(since: dt.date, *, client: httpx.AsyncClient) -> dict[str, Any]:
    """GET /api/quality-issues?since=...&include_ignored=false&limit=500."""
    try:
        response = await client.get(
            QUALITY_ISSUES_PATH,
            params={"since": since.isoformat(), "include_ignored": "false", "limit": 500},
        )
    except httpx.ConnectError as exc:
        raise ProductionBriefError(
            f"Could not reach the production brief at {client.base_url}."
        ) from exc
    except httpx.TimeoutException as exc:
        raise ProductionBriefError(
            f"Timed out waiting for the production brief at {client.base_url}."
        ) from exc

    if response.status_code != 200:
        raise ProductionBriefError(
            f"Production brief returned HTTP {response.status_code} for {QUALITY_ISSUES_PATH}."
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise ProductionBriefError("Production brief returned malformed JSON.") from exc

    return validate_raw_payload(data)


def _parse_piece_count(cost_note: str | None) -> int:
    if not cost_note:
        return 1
    match = _PIECE_COUNT_RE.search(cost_note)
    if not match:
        return 1
    parsed = int(match.group(1))
    return parsed if parsed >= 1 else 1


def _normalize_subcategory(subcategory: str | None) -> str:
    """The real production brief uses hyphens ("wrong-size", "finish-quality");
    SUBCATEGORY_TO_CATEGORY_NAME's keys are space-separated - normalize both
    hyphens and underscores to spaces before matching so real data maps
    correctly instead of silently falling through to "Other" (found via a real
    sync against real data - see docs/PROJECT_SPEC_PHASE3.md)."""
    return (subcategory or "").strip().lower().replace("-", " ").replace("_", " ")


def _map_category_id(db: Session, subcategory: str | None) -> int:
    name = SUBCATEGORY_TO_CATEGORY_NAME.get(_normalize_subcategory(subcategory), "Other")
    category = db.query(CustomerIssueCategory).filter(CustomerIssueCategory.name == name).first()
    if category is None:
        # "Other" itself is always seeded (app/seed_data.py), but fall back
        # defensively in case master data was edited unexpectedly.
        category = db.query(CustomerIssueCategory).order_by(CustomerIssueCategory.id).first()
    if category is None:
        raise ProductionBriefError(
            "No CustomerIssueCategory rows exist - run seed_master_data first."
        )
    return category.id


def map_issue_fields(db: Session, raw: dict[str, Any]) -> dict[str, Any]:
    """Translate one production-brief issue dict into CustomerIssue field values."""
    thread_id = raw.get("thread_id")
    if not thread_id:
        raise ProductionBriefError("Issue is missing required 'thread_id'.")

    day = raw.get("day")
    if not day:
        raise ProductionBriefError(f"Issue {thread_id} is missing required 'day'.")
    reported_date = dt.date.fromisoformat(day)

    piece_count = _parse_piece_count(raw.get("cost_note"))
    rework_cost = raw.get("rework_cost")
    estimated_rework_cost = (
        decimal.Decimal(str(rework_cost))
        if rework_cost is not None
        else customer_issue_service.BASE_REWORK_COST_PER_PIECE * piece_count
    )

    source_type = BRIEF_CATEGORY_TO_SOURCE_TYPE.get(
        (raw.get("category") or "").strip().lower(), "Manufacturing"
    )

    return {
        "source_thread_id": thread_id,
        "reported_date": reported_date,
        "customer_name": (raw.get("customer") or "Unknown customer").strip(),
        "order_number": raw.get("order_no"),
        "issue_category_id": _map_category_id(db, raw.get("subcategory")),
        "source_type": source_type,
        "should_have_caught_at": raw.get("station"),
        "piece_count": piece_count,
        "estimated_rework_cost": estimated_rework_cost,
        "description": raw.get("summary") or "",
        "photo_urls": raw.get("photos_json"),
        "status": "Ignored" if raw.get("ignored") else "Open",
        "needs_review_note": NEEDS_REVIEW_NOTE if raw.get("needs_review") else None,
    }


def _apply_fields_to_new_issue(issue: CustomerIssue, fields: dict[str, Any]) -> None:
    issue.source_thread_id = fields["source_thread_id"]
    issue.reported_date = fields["reported_date"]
    issue.customer_name = fields["customer_name"]
    issue.order_number = fields["order_number"]
    issue.issue_category_id = fields["issue_category_id"]
    issue.source_type = fields["source_type"]
    issue.should_have_caught_at = fields["should_have_caught_at"]
    issue.piece_count = fields["piece_count"]
    issue.estimated_rework_cost = fields["estimated_rework_cost"]
    issue.description = fields["description"]
    issue.photo_urls = fields["photo_urls"]
    issue.status = fields["status"]
    if fields["needs_review_note"]:
        issue.notes = fields["needs_review_note"]


def _apply_fields_to_existing_issue(issue: CustomerIssue, fields: dict[str, Any]) -> None:
    """Update a previously-synced issue with fresh data from the brief, while
    preserving anything staff did locally: linked_defect_case_id, a status that has
    already moved past "Open" (e.g. staff linked it, or a prior sync/UI action
    ignored it), and any notes staff typed in.
    """
    issue.reported_date = fields["reported_date"]
    issue.customer_name = fields["customer_name"]
    issue.order_number = fields["order_number"]
    issue.issue_category_id = fields["issue_category_id"]
    issue.source_type = fields["source_type"]
    issue.should_have_caught_at = fields["should_have_caught_at"]
    issue.piece_count = fields["piece_count"]
    issue.estimated_rework_cost = fields["estimated_rework_cost"]
    issue.description = fields["description"]
    issue.photo_urls = fields["photo_urls"]

    # Only let the brief move a still-untouched "Open" issue to "Ignored" - never
    # downgrade a status staff has already acted on (Linked, or a prior Ignored).
    if issue.status == "Open":
        issue.status = fields["status"]

    note = fields["needs_review_note"]
    if note and (not issue.notes or note not in issue.notes):
        issue.notes = f"{note} {issue.notes}" if issue.notes else note


def _default_since(db: Session) -> dt.date:
    last_success = (
        db.query(SyncLog)
        .filter(SyncLog.status == "success")
        .order_by(SyncLog.sync_completed_at.desc())
        .first()
    )
    if last_success is not None and last_success.sync_completed_at is not None:
        return last_success.sync_completed_at.date() - dt.timedelta(days=SINCE_LOOKBACK_BUFFER_DAYS)
    # First-ever sync: bootstrap with a generous window rather than "today only".
    return dt.date.today() - dt.timedelta(days=90)


def _get_setting_datetime(db: Session, key: str) -> dt.datetime | None:
    """Generic ISO-datetime-string reader for the AppSetting rows above, mirroring
    app/services/auth_service.py's get_app_username()/get_app_password_hash() -
    small dedicated helpers over the generic key-value table rather than a bespoke
    column. Missing row or empty string both mean "never"."""
    setting = db.get(AppSetting, key)
    if setting is None or not setting.value:
        return None
    try:
        value = dt.datetime.fromisoformat(setting.value)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def _set_setting_datetime(db: Session, key: str, value: dt.datetime | None) -> None:
    """Write (or clear, with value=None) one of the ISO-datetime AppSetting rows
    above. Does not commit - callers compose this into their own transaction, same
    as auth_service.sync_credentials_from_env()."""
    raw = value.astimezone(dt.timezone.utc).isoformat() if value is not None else ""
    setting = db.get(AppSetting, key)
    if setting is None:
        db.add(AppSetting(key=key, value=raw))
    else:
        setting.value = raw


def get_manual_sync_requested_at(db: Session) -> dt.datetime | None:
    return _get_setting_datetime(db, MANUAL_SYNC_REQUESTED_SETTING_KEY)


def is_manual_sync_pending(db: Session) -> bool:
    return get_manual_sync_requested_at(db) is not None


def request_manual_sync(db: Session) -> dt.datetime:
    """ "Sync Now" button (POST /api/v1/sync/customer-issues/request-manual-sync):
    record that a manual sync is wanted and return immediately - no network call,
    just a DB write, so this always succeeds instantly. The actual fetch+ingest
    happens later, asynchronously, when the local relay's frequent heartbeat (GET
    /api/v1/sync/customer-issues/relay-status) next sees this flag set and performs
    a full relay pass. Cleared by process_issues_payload() on the next successful
    ingest, whichever path that comes through."""
    now = dt.datetime.now(dt.timezone.utc)
    _set_setting_datetime(db, MANUAL_SYNC_REQUESTED_SETTING_KEY, now)
    db.commit()
    return now


def clear_manual_sync_request(db: Session) -> None:
    """Clear the pending-manual-sync flag. Called unconditionally from
    process_issues_payload() on every successful sync (relay-ingested or direct) -
    a clear when nothing was pending is a harmless no-op, and doing it here (rather
    than as a separate step the relay/UI has to remember) guarantees the flag can
    never get stuck showing "pending" after the work it asked for already
    happened. Does not commit - composes into the caller's transaction."""
    _set_setting_datetime(db, MANUAL_SYNC_REQUESTED_SETTING_KEY, None)


def get_relay_last_seen_at(db: Session) -> dt.datetime | None:
    return _get_setting_datetime(db, RELAY_LAST_SEEN_SETTING_KEY)


def record_relay_heartbeat(db: Session) -> bool:
    """GET /api/v1/sync/customer-issues/relay-status side effect: stamp
    sync_relay_last_seen_at as now (this call IS the heartbeat the Customer Issues
    tab's connected/disconnected status line is computed from - see
    relay_connection_status), then report whether a manual sync is currently
    pending so the relay's frequent check-in knows whether to also run a full
    fetch+ingest this cycle. Cheap and fast - no network call to the production
    brief happens here, just a DB read+write."""
    _set_setting_datetime(db, RELAY_LAST_SEEN_SETTING_KEY, dt.datetime.now(dt.timezone.utc))
    pending = is_manual_sync_pending(db)
    db.commit()
    return pending


def relay_connection_status(db: Session, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """Shared computation behind the Customer Issues tab's heartbeat status line:
    connected iff the relay's last heartbeat (record_relay_heartbeat) was within
    RELAY_HEARTBEAT_STALE_AFTER of `now`; never-seen counts as not connected, same
    as a stale one. `now` is injectable for tests."""
    last_seen = get_relay_last_seen_at(db)
    effective_now = now or dt.datetime.now(dt.timezone.utc)
    connected = last_seen is not None and (effective_now - last_seen) <= RELAY_HEARTBEAT_STALE_AFTER
    return {
        "relay_last_seen_at": last_seen,
        "relay_connected": connected,
        "manual_sync_pending": is_manual_sync_pending(db),
    }


def process_issues_payload(
    db: Session,
    data: dict[str, Any],
    *,
    source_url: str,
    sync_started_at: dt.datetime | None = None,
) -> SyncLog:
    """Turn an already-fetched raw payload (the same shape fetch_issues() returns -
    a dict with an "issues" list) into CustomerIssue rows: map, dedup by
    source_thread_id, upsert, and record the outcome as a SyncLog row.

    This is the ONE place the per-issue mapping/dedup/upsert logic lives. Both
    run_sync() (the hourly direct-fetch path and the "Sync Now" API route) and the
    relay ingest endpoint (POST /api/v1/sync/customer-issues/ingest-raw) call this
    after obtaining `data` by different means (a direct HTTP fetch vs. a request
    body forwarded by the local relay script) - neither duplicates this loop.

    Does NOT validate `data`'s shape itself (see validate_raw_payload) - callers
    that receive `data` from an untrusted source (e.g. an API request body) should
    validate it first and turn a shape mismatch into their own clean error response,
    rather than have it show up here as a merely-logged, per-record skip.

    Never raises for a bad individual record - it's skipped, counted, and logged in
    SyncLog.errors so one malformed issue can't abort the whole batch.
    """
    log = SyncLog(
        sync_started_at=sync_started_at or dt.datetime.now(dt.timezone.utc),
        source_url=source_url,
        status="failed",
    )

    issues = data.get("issues") or []
    created = updated = skipped = 0
    error_messages: list[str] = []

    for raw in issues:
        try:
            fields = map_issue_fields(db, raw)
        except Exception as exc:  # noqa: BLE001 - one bad record must not abort the batch
            skipped += 1
            error_messages.append(f"{raw.get('thread_id', '?')}: {exc}")
            logger.warning("Skipped a production brief issue: %s", exc)
            continue

        existing = (
            db.query(CustomerIssue)
            .filter(CustomerIssue.source_thread_id == fields["source_thread_id"])
            .first()
        )
        if existing is not None:
            _apply_fields_to_existing_issue(existing, fields)
            updated += 1
        else:
            issue = CustomerIssue(
                issue_number=customer_issue_service.generate_issue_number(
                    db, fields["reported_date"]
                )
            )
            _apply_fields_to_new_issue(issue, fields)
            db.add(issue)
            db.flush()  # so the next generate_issue_number() sees this row
            created += 1

    log.records_fetched = len(issues)
    log.records_created = created
    log.records_updated = updated
    log.records_skipped = skipped
    log.errors = "; ".join(error_messages) or None
    log.status = "success"
    log.sync_completed_at = dt.datetime.now(dt.timezone.utc)
    # A successful sync (whichever path produced it) is the actual work a pending
    # "Sync Now" request was asking for - clear it here rather than leaving it to
    # the relay/UI to remember as a separate step.
    clear_manual_sync_request(db)
    db.add(log)
    db.commit()
    db.refresh(log)
    logger.info(
        "Customer issue sync complete: %d fetched, %d created, %d updated, %d skipped",
        log.records_fetched,
        created,
        updated,
        skipped,
    )
    return log


async def run_sync(db: Session, *, since: dt.date | None = None) -> SyncLog:
    """Pull issues since the last successful sync minus a lookback buffer (see
    SINCE_LOOKBACK_BUFFER_DAYS), or a 90-day bootstrap window on the first run, then
    hand off to process_issues_payload() for the mapping/dedup/upsert work.

    Never raises - a failure to reach the production brief is recorded in the
    returned SyncLog (status="failed", errors=<message>) so callers (the hourly
    background task, the "Sync Now" API route) can treat every outcome uniformly.
    """
    settings = get_settings()
    source_url = f"{settings.production_brief_url}{QUALITY_ISSUES_PATH}"
    effective_since = since if since is not None else _default_since(db)
    sync_started_at = dt.datetime.now(dt.timezone.utc)

    try:
        async with _build_client() as client:
            data = await fetch_issues(effective_since, client=client)
    except ProductionBriefError as exc:
        logger.error("Customer issue sync failed: %s", exc)
        log = SyncLog(
            sync_started_at=sync_started_at,
            source_url=source_url,
            status="failed",
            errors=str(exc),
            sync_completed_at=dt.datetime.now(dt.timezone.utc),
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        return log

    return process_issues_payload(db, data, source_url=source_url, sync_started_at=sync_started_at)


async def run_periodic_sync(interval_minutes: int, session_factory=None) -> None:
    """Runs one sync immediately, then repeats every interval_minutes, forever.

    No longer scheduled automatically by app.main's lifespan - Render cannot reach
    the production brief directly, so every automatic run of this on Render always
    failed. Left in place (not deleted) for a future local-network deployment or
    manual debugging; call it yourself (e.g. from a shell) if you need it. Never
    raises - errors are logged and the loop continues (matching the original spec:
    "If the production brief is unreachable, log the error and retry on the next
    interval").
    """
    factory = session_factory or SessionLocal
    while True:
        db = factory()
        try:
            await run_sync(db)
        except Exception:  # noqa: BLE001 - the loop must survive any unexpected error
            logger.exception("Unexpected error during periodic customer issue sync")
        finally:
            db.close()
        await asyncio.sleep(interval_minutes * 60)
