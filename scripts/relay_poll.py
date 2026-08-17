#!/usr/bin/env python
"""Frequent local relay check-in for Customer Issues sync (Phase 3 follow-up).

Render's servers cannot reach the Eagle production brief directly (see
scripts/relay_customer_issues.py's docstring), so the Customer Issues tab's "Sync
Now" button no longer tries a direct fetch from Render - it always failed there.
Instead, clicking it just records a "manual sync requested" flag server-side
(POST /api/v1/sync/customer-issues/request-manual-sync) and returns instantly.

This script is the other half of that: intended to run on a frequent (~1 minute)
Windows Task Scheduler interval, it polls the cheap heartbeat endpoint
GET /api/v1/sync/customer-issues/relay-status (a DB-only read+write on the server -
no call to the production brief happens there) and, ONLY when that response says a
manual sync is pending, performs the exact same full fetch+ingest as the existing
hourly relay by calling relay_customer_issues.run() directly - never reimplementing
that fetch/ingest HTTP logic here (see that module's docstring: "deliberately a
dumb pipe").

The existing hourly full relay (scripts/relay_customer_issues.py, registered as its
own separate scheduled task) is untouched by this script and keeps running exactly
as before: an unconditional fetch+ingest every hour, unrelated to the pending flag.

Logging is deliberately sparse. This script is expected to run about once a
minute, indefinitely, so logging every routine "nothing pending" check-in would
make logs/relay_customer_issues.log grow without bound for very little value.
Only three things get logged here:
  - a pending sync being found and acted on (the SUCCESS/FAILURE line for the
    relay pass itself is logged by relay_customer_issues.run(), reused as-is);
  - this heartbeat call itself failing (production brief involvement: none -
    Render unreachable, bad response, missing config);
  - any unexpected exception.
A long stretch of silence in the log is expected and fine when nothing is pending -
the heartbeat's own health is visible server-side via sync_relay_last_seen_at (and
therefore the Customer Issues tab's connected/disconnected status line), not by
tailing this log.

Usage:
    python scripts/relay_poll.py

Reads the same PRODUCTION_BRIEF_URL / RELAY_API_KEY (via app.config) and RENDER_URL
(raw environment variable) as relay_customer_issues.py - see that script's
docstring for details. PRODUCTION_BRIEF_URL itself is only actually used if a
manual sync turns out to be pending (relay_customer_issues.run() reads it).

On any failure reaching the heartbeat endpoint (Render unreachable, non-200,
missing config), logs a short summary and exits with code 1 - never an unhandled
traceback - so a scheduled run never shows up as a crashed task; the next scheduled
run just tries again. Exit code 0 means the check-in itself succeeded, whether or
not anything was pending. If a pending sync WAS found and acted on, the exit code
instead reflects relay_customer_issues.run()'s own outcome (0 success, 1 failure).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

import relay_customer_issues  # noqa: E402

from app.config import get_settings  # noqa: E402

RELAY_STATUS_PATH = "/api/v1/sync/customer-issues/relay-status"
HEARTBEAT_TIMEOUT_SECONDS = 15.0


def check_in() -> int:
    """One heartbeat cycle: GET relay-status, and if it reports a pending manual
    sync, run the full existing relay (fetch from the production brief, POST to
    ingest-raw) via relay_customer_issues.run(). Returns a process exit code."""
    settings = get_settings()
    render_url = os.getenv("RENDER_URL", "").strip().rstrip("/")
    relay_api_key = settings.relay_api_key

    if not render_url:
        relay_customer_issues._log("POLL FAILURE: RENDER_URL is not set - check your local .env.")
        return 1
    if not relay_api_key:
        relay_customer_issues._log(
            "POLL FAILURE: RELAY_API_KEY is not set - check your local .env."
        )
        return 1

    try:
        resp = httpx.get(
            f"{render_url}{RELAY_STATUS_PATH}",
            headers={"X-Relay-Key": relay_api_key},
            timeout=HEARTBEAT_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        relay_customer_issues._log(
            f"POLL FAILURE: could not reach the Render app at {render_url}: {exc}"
        )
        return 1

    if resp.status_code != 200:
        relay_customer_issues._log(
            f"POLL FAILURE: Render app returned HTTP {resp.status_code} for "
            f"{RELAY_STATUS_PATH}."
        )
        return 1

    try:
        body = resp.json()
    except ValueError:
        relay_customer_issues._log(
            "POLL FAILURE: Render app returned malformed JSON from the heartbeat endpoint."
        )
        return 1

    if not body.get("manual_sync_pending"):
        # Deliberately not logged - see the module docstring's note on log
        # verbosity for a once-a-minute call running indefinitely.
        return 0

    relay_customer_issues._log("Manual sync pending - running full relay now.")
    return relay_customer_issues.run()


def main() -> int:
    try:
        return check_in()
    except Exception as exc:  # noqa: BLE001 - a scheduled run must never crash
        relay_customer_issues._log(f"POLL FAILURE: unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
