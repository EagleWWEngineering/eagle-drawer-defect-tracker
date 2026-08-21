#!/usr/bin/env python
"""Local relay for Customer Issues sync (Phase 3) + Daily Schedule sync (Phase 6).

Render's servers cannot reach the Eagle production brief directly - a firewall on
the production brief's side blocks it (confirmed via a connection timeout from
Render's own shell). Rodolfo's local machine CAN reach it. This script bridges the
gap: it fetches the raw production-brief data here, then forwards it to the live
Render app's ingest endpoints, which do all the real processing.

This one process now does TWO independent fetch+forward passes each run:

  1. Customer issues (unchanged since Phase 3): fetch GET /api/quality-issues,
     forward the raw JSON body as-is to POST .../customer-issues/ingest-raw, which
     does the exact same field-mapping/dedup/category-matching logic as the
     existing hourly direct-fetch sync (see app/services/sync_service.py
     process_issues_payload()). This script is deliberately a dumb pipe for that
     data - it must never contain any field-mapping, dedup, or category logic of
     its own. If you're tempted to add any, it belongs in sync_service.py instead.

  2. Daily schedule (Phase 6, new): scrape "drawers scheduled to finish today" out
     of the production brief's rendered drawers.html board - there is no JSON API
     for this number (see docs/PRODUCTION_BRIEF_SCHEDULE_SOURCE.md for the full
     discovery writeup) - for today plus a trailing SCHEDULE_LOOKBACK_DAYS window
     (via the brief's dated /archive/<date>/drawers.html pages), then forward the
     scraped {date: count} map to POST .../daily-schedule/ingest-raw. UNLIKE the
     customer-issues path, this one genuinely has to scrape HTML - that scraping
     is isolated to _scrape_drawers_scheduled() below (the ONE function to swap
     out if/when Blake adds a real JSON endpoint to eagle-production-brief;
     nothing else in this script would need to change).

These two passes are fully independent: a failure in one (production brief down,
Render down, bad response) must never prevent the other from running and being
logged. See run() below - each pass is wrapped separately and both always run.

This module's run()/_log() are also imported directly by scripts/relay_poll.py (a
companion script, run on a much more frequent ~1 minute Task Scheduler interval)
so that its "a manual Sync Now was requested - do a full relay pass right now"
behavior reuses this exact fetch+ingest code path instead of duplicating it. This
script's own unconditional hourly schedule is unaffected by that and keeps running
exactly as before.

Usage:
    python scripts/relay_customer_issues.py

Reads from a local .env (via app.config, same .env the FastAPI app itself uses):
    PRODUCTION_BRIEF_URL - the production brief's base URL (already used by the
                           existing hourly direct-fetch sync).
    RELAY_API_KEY        - shared secret also configured on Render, checked by
                           both POST .../ingest-raw endpoints below. One secret,
                           reused for both passes - never a new one per feature.

Reads directly from the environment (not part of the FastAPI app's own Settings,
since the app itself never needs to know its own external URL):
    RENDER_URL            - the live Render app's base URL, e.g.
                           https://your-app.onrender.com.

Logs to logs/relay_customer_issues.log (created if missing), plus stdout. Overall
exit code is 0 only if BOTH passes succeeded, 1 if either failed - but a failure in
one never stops the other from running; see run(). Never an unhandled traceback -
so a scheduled run never shows up as a crashed task; the next scheduled run just
tries again.
"""

from __future__ import annotations

import datetime as dt
import html as html_lib
import os
import re
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

# --- Phase 6: daily schedule scrape --------------------------------------------
#
# See docs/PRODUCTION_BRIEF_SCHEDULE_SOURCE.md for the full discovery writeup:
# the production brief is a stdlib http.server app, not a framework with routes -
# there is no JSON endpoint for "drawers scheduled to finish today", only the
# rendered drawers.html board (today) and dated archive copies (past dates).
SCHEDULE_INGEST_PATH = "/api/v1/sync/daily-schedule/ingest-raw"
DRAWERS_BOARD_PATH = "/drawers.html"

# Overwrite today's value on every hourly run (the schedule may change during the
# day per the task spec - in practice the brief snapshots it once at ~06:15 ET and
# it won't actually change intraday, but re-sending the same number hourly is
# harmless - see the doc). Once a date rolls over, that date is never re-fetched
# except within this trailing window, which exists purely so a missed hourly run
# self-heals on the next one.
SCHEDULE_LOOKBACK_DAYS = 7

_FACT_RE = re.compile(
    r'<div class="fact fact-[a-z]+">'
    r'<div class="fact-value">([\d,]+)</div>'
    r'<div class="fact-label">([^<]*)</div>'
    r"</div>"
)
_SCHEDULE_FACT_LABEL = "drawers scheduled to finish today"


def _scrape_drawers_scheduled(page_html: str) -> int | None:
    """Isolated, single-purpose HTML scrape (see the module docstring and
    docs/PRODUCTION_BRIEF_SCHEDULE_SOURCE.md for why this exists instead of a
    JSON API call). Nothing else in this script parses HTML - if you're extending
    what's scraped, keep it contained to a function like this one.

    Looks for the exact markup production_brief/render.py's _facts() emits: pairs
    of <div class="fact-value">N</div><div class="fact-label">...</div> inside a
    <div class="fact ..."> wrapper. Matches the fact whose label is exactly
    "drawers scheduled to finish today" (case-insensitive, HTML-entity-decoded).

    Returns None - not 0 - if that fact isn't present at all: a day the brief had
    no "Today's plan" snapshot for (before the feature existed, or state
    genuinely missing), which is a different fact from "scheduled zero drawers".
    """
    for value_str, label_raw in _FACT_RE.findall(page_html):
        label = html_lib.unescape(label_raw).strip().lower()
        if label == _SCHEDULE_FACT_LABEL:
            try:
                return int(value_str.replace(",", ""))
            except ValueError:
                return None
    return None


def _fetch_drawers_board_html(base_url: str, day: dt.date, *, today: dt.date) -> tuple[str, str]:
    """("ok" | "not_found" | "unreachable", html_or_error). Today's board has no
    query-param API for past dates (see the doc) - it always renders "today", so a
    past date is fetched from the brief's dated archive copy instead."""
    path = DRAWERS_BOARD_PATH if day == today else f"/archive/{day.isoformat()}{DRAWERS_BOARD_PATH}"
    try:
        resp = httpx.get(f"{base_url}{path}", timeout=FETCH_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        return "unreachable", str(exc)
    if resp.status_code != 200:
        # Expected/normal for dates before the archive (or the plan-snapshot
        # feature) existed - not a connectivity problem. See the doc's "How far
        # back" section.
        return "not_found", f"HTTP {resp.status_code}"
    return "ok", resp.text


def _collect_schedule_entries(base_url: str) -> tuple[list[dict], int, int]:
    """(entries, found_count, unreachable_count) for today + a trailing
    SCHEDULE_LOOKBACK_DAYS window. `entries` is ready to drop straight into the
    ingest payload's "schedules" list."""
    today = dt.date.today()
    entries: list[dict] = []
    found = 0
    unreachable = 0
    for back in range(SCHEDULE_LOOKBACK_DAYS + 1):
        day = today - dt.timedelta(days=back)
        status, body = _fetch_drawers_board_html(base_url, day, today=today)
        if status == "ok":
            count = _scrape_drawers_scheduled(body)
            if count is not None:
                found += 1
        else:
            count = None
            if status == "unreachable":
                unreachable += 1
        entries.append({"date": day.isoformat(), "drawers_scheduled": count})
    return entries, found, unreachable


def _log(message: str) -> None:
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    line = f"{timestamp} {message}"
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def _run_customer_issues_forward(
    production_brief_url: str, render_url: str, relay_api_key: str
) -> bool:
    """Unchanged Phase 3 behavior, extracted from the old top-level run() so it can
    be called independently of the Phase 6 schedule forward below. True on
    success, False on any failure - never raises."""
    since = (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()

    try:
        fetch_resp = httpx.get(
            f"{production_brief_url}{QUALITY_ISSUES_PATH}",
            params={"since": since, "include_ignored": "false", "limit": 500},
            timeout=FETCH_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        _log(
            f"ISSUES FAILURE: could not reach the production brief at "
            f"{production_brief_url}: {exc}"
        )
        return False

    if fetch_resp.status_code != 200:
        _log(
            f"ISSUES FAILURE: production brief returned HTTP {fetch_resp.status_code} "
            f"for {QUALITY_ISSUES_PATH}."
        )
        return False

    try:
        payload = fetch_resp.json()
    except ValueError:
        _log("ISSUES FAILURE: production brief returned malformed JSON.")
        return False

    issue_count = len(payload.get("issues") or []) if isinstance(payload, dict) else "?"

    try:
        ingest_resp = httpx.post(
            f"{render_url}{INGEST_PATH}",
            json=payload,
            headers={"X-Relay-Key": relay_api_key},
            timeout=INGEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        _log(f"ISSUES FAILURE: could not reach the Render app at {render_url}: {exc}")
        return False

    if ingest_resp.status_code != 200:
        _log(
            f"ISSUES FAILURE: Render app returned HTTP {ingest_resp.status_code} for "
            f"{INGEST_PATH}: {ingest_resp.text[:500]}"
        )
        return False

    try:
        result = ingest_resp.json()
    except ValueError:
        result = {}

    _log(
        f"ISSUES SUCCESS: fetched {issue_count} issue(s) from the production brief; "
        f"ingest reported {result.get('records_created')} created, "
        f"{result.get('records_updated')} updated, "
        f"{result.get('records_skipped')} skipped."
    )
    return True


def _run_schedule_forward(production_brief_url: str, render_url: str, relay_api_key: str) -> bool:
    """Phase 6: scrape + forward the drawers-scheduled figures. Independent of
    _run_customer_issues_forward - failures here never affect that pass, and vice
    versa (see run()). True on success, False on any failure - never raises."""
    entries, found, unreachable = _collect_schedule_entries(production_brief_url)

    if unreachable == len(entries):
        # Every single fetch failed at the connection level - the production brief
        # itself is down/unreachable, same failure class as the issues pass above.
        # A partial mix of "ok" + expected 404s (dates before the archive existed)
        # is normal and NOT treated as a failure - see _fetch_drawers_board_html.
        _log(
            f"SCHEDULE FAILURE: could not reach the production brief at "
            f"{production_brief_url} for any of {len(entries)} date(s)."
        )
        return False

    payload = {"schedules": entries}

    try:
        ingest_resp = httpx.post(
            f"{render_url}{SCHEDULE_INGEST_PATH}",
            json=payload,
            headers={"X-Relay-Key": relay_api_key},
            timeout=INGEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        _log(f"SCHEDULE FAILURE: could not reach the Render app at {render_url}: {exc}")
        return False

    if ingest_resp.status_code != 200:
        _log(
            f"SCHEDULE FAILURE: Render app returned HTTP {ingest_resp.status_code} for "
            f"{SCHEDULE_INGEST_PATH}: {ingest_resp.text[:500]}"
        )
        return False

    try:
        result = ingest_resp.json()
    except ValueError:
        result = {}

    _log(
        f"SCHEDULE SUCCESS: scraped {found}/{len(entries)} day(s) in the trailing window "
        f"({unreachable} unreachable); ingest reported {result.get('records_created')} "
        f"created, {result.get('records_updated')} updated, "
        f"{result.get('records_skipped')} skipped."
    )
    return True


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

    # Both passes always run, independently, regardless of whether the other
    # succeeded or failed - see the module docstring.
    issues_ok = _run_customer_issues_forward(production_brief_url, render_url, relay_api_key)
    schedule_ok = _run_schedule_forward(production_brief_url, render_url, relay_api_key)

    return 0 if (issues_ok and schedule_ok) else 1


def main() -> int:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 - a scheduled run must never crash
        _log(f"FAILURE: unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
