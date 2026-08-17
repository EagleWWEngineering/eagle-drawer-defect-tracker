#!/usr/bin/env python
"""Local relay for Customer Issues sync (Phase 3).

Render's servers cannot reach the Eagle production brief directly - a firewall on
the production brief's side blocks it (confirmed via a connection timeout from
Render's own shell). Rodolfo's local machine CAN reach it. This script bridges the
gap: it fetches the raw production-brief payload here, then forwards it, unmodified,
to the live Render app's ingest endpoint, which does all the real processing using
the exact same field-mapping/dedup/category-matching logic as the existing hourly
direct-fetch sync (see app/services/sync_service.py process_issues_payload()).

This script is deliberately a dumb pipe - it must never contain any field-mapping,
dedup, or category logic of its own. If you're tempted to add any, it belongs in
app/services/sync_service.py instead, shared by both sync paths.

Usage:
    python scripts/relay_customer_issues.py

Reads from a local .env (via app.config, same .env the FastAPI app itself uses):
    PRODUCTION_BRIEF_URL - the production brief's base URL (already used by the
                           existing hourly direct-fetch sync).
    RELAY_API_KEY        - shared secret also configured on Render, checked by
                           POST /api/v1/sync/customer-issues/ingest-raw.

Reads directly from the environment (not part of the FastAPI app's own Settings,
since the app itself never needs to know its own external URL):
    RENDER_URL            - the live Render app's base URL, e.g.
                           https://your-app.onrender.com.

Logs one line per run to logs/relay_customer_issues.log (created if missing),
plus stdout. On ANY failure (production brief unreachable, Render unreachable,
non-200 from either, missing config), it logs a short summary and exits with code 1
- never an unhandled traceback - so a scheduled run never shows up as a crashed
task; the next scheduled run just tries again. Exit code 0 means success.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402

QUALITY_ISSUES_PATH = "/api/quality-issues"
INGEST_PATH = "/api/v1/sync/customer-issues/ingest-raw"
LOG_PATH = PROJECT_ROOT / "logs" / "relay_customer_issues.log"

# A simple fixed lookback window, local to this script. The server-side sync has a
# DB-backed "since last successful sync" cursor (sync_service._default_since()),
# but this standalone script has no database of its own to read that from, so it
# just re-checks a generous trailing window on every run instead. Harmless: issues
# are upserted by source_thread_id server-side, so re-sending the same issue twice
# is idempotent (same reasoning as sync_service.SINCE_LOOKBACK_BUFFER_DAYS).
LOOKBACK_DAYS = 7

FETCH_TIMEOUT_SECONDS = 30.0
INGEST_TIMEOUT_SECONDS = 60.0


def _log(message: str) -> None:
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    line = f"{timestamp} {message}"
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def run() -> int:
    settings = get_settings()
    render_url = os.getenv("RENDER_URL", "").strip().rstrip("/")
    relay_api_key = settings.relay_api_key
    production_brief_url = settings.production_brief_url

    if not render_url:
        _log("FAILURE: RENDER_URL is not set - check your local .env.")
        return 1
    if not relay_api_key:
        _log("FAILURE: RELAY_API_KEY is not set - check your local .env.")
        return 1

    since = (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()

    try:
        fetch_resp = httpx.get(
            f"{production_brief_url}{QUALITY_ISSUES_PATH}",
            params={"since": since, "include_ignored": "false", "limit": 500},
            timeout=FETCH_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        _log(f"FAILURE: could not reach the production brief at {production_brief_url}: {exc}")
        return 1

    if fetch_resp.status_code != 200:
        _log(
            f"FAILURE: production brief returned HTTP {fetch_resp.status_code} "
            f"for {QUALITY_ISSUES_PATH}."
        )
        return 1

    try:
        payload = fetch_resp.json()
    except ValueError:
        _log("FAILURE: production brief returned malformed JSON.")
        return 1

    issue_count = len(payload.get("issues") or []) if isinstance(payload, dict) else "?"

    try:
        ingest_resp = httpx.post(
            f"{render_url}{INGEST_PATH}",
            json=payload,
            headers={"X-Relay-Key": relay_api_key},
            timeout=INGEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        _log(f"FAILURE: could not reach the Render app at {render_url}: {exc}")
        return 1

    if ingest_resp.status_code != 200:
        _log(
            f"FAILURE: Render app returned HTTP {ingest_resp.status_code} for "
            f"{INGEST_PATH}: {ingest_resp.text[:500]}"
        )
        return 1

    try:
        result = ingest_resp.json()
    except ValueError:
        result = {}

    _log(
        f"SUCCESS: fetched {issue_count} issue(s) from the production brief; "
        f"ingest reported {result.get('records_created')} created, "
        f"{result.get('records_updated')} updated, "
        f"{result.get('records_skipped')} skipped."
    )
    return 0


def main() -> int:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 - a scheduled run must never crash
        _log(f"FAILURE: unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
