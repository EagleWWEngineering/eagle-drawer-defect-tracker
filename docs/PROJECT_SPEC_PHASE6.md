# Phase 6 Addendum — Scheduled vs Completed Drawers

Addendum to [`PROJECT_SPEC.md`](PROJECT_SPEC.md) and the Phase 2/3/4/5 addenda.
Everything in all of them still applies unchanged. This document covers only what
Phase 6 added: visibility into how many drawers were **scheduled** to finish each
day (from the Eagle production brief) versus how many were **actually completed**
(`drawers_inspected`, entered on the Daily Production Summary — the existing proxy
for "actually completed"). Phase 6 does not touch defect dispositions, statuses, or
cost — those are unrelated, separate work.

## Purpose and scope

Rodolfo wants, at a glance: did the floor keep up with the plan? The production
brief (`Eagle-Woodworking/eagle-production-brief`, a separate repo Blake owns)
already computes a daily plan number — "drawers scheduled to finish today" — for
its own TV board. Phase 6 pulls that number into this app so it can sit next to
`drawers_inspected` on the Dashboard, in Reports, and in the CSV export.

## Data source: no JSON API exists

Full discovery writeup: [`PRODUCTION_BRIEF_SCHEDULE_SOURCE.md`](PRODUCTION_BRIEF_SCHEDULE_SOURCE.md).
Short version: the production brief is a stdlib `http.server` app, not a
framework with routes — there is no `/openapi.json`, no `/docs`, and no JSON
endpoint for this number. It has to be scraped out of the rendered `drawers.html`
board (today) or a dated archive copy (`/archive/<YYYY-MM-DD>/drawers.html`, past
dates) — the exact `<div class="fact">` markup `production_brief/render.py`
emits. This is flagged there as technical debt for Blake to eventually replace
with a real endpoint; nothing in this app assumes that will happen soon.

## Data model

### DailySchedule (`daily_schedules`)
| Field | Type | Notes |
|---|---|---|
| production_date | date, primary key | one row per calendar date — a whole-day figure, unlike `daily_production_summaries` which is per-shift |
| drawers_scheduled | int >= 0 | |
| source | string, `"sync"` \| `"manual"` | which side last wrote this row |
| synced_at | datetime (UTC), nullable | last successful relay write; null if this date has never been synced |
| updated_at | datetime (UTC) | |

Deliberately a separate table from `daily_production_summaries`, not a new column
on it — see `app/models.py`'s `DailySchedule` docstring. A two-shift day has two
`DailyProductionSummary` rows but exactly one schedule figure; putting it on the
summary row would either double-count on `SUM()` or force every query to dedupe
by date.

**Manual-wins rule:** if a row's `source == "manual"`, the relay sync skips that
date entirely — the human-entered value and its `"manual"` source are left
completely untouched. Only `source == "sync"` rows (and dates with no row at all)
get overwritten by the relay. Enforced in exactly one place:
`app/services/schedule_service.py upsert_schedule()` / `_apply_schedule()`.

## Service layer (`app/services/schedule_service.py`)

- `get_schedule(db, production_date) -> DailySchedule | None`
- `get_schedules_in_range(db, start, end) -> dict[date, int]` / `list_schedules_in_range` / `list_schedules` (optional bounds)
- `upsert_schedule(db, production_date, drawers_scheduled, source)` — the
  manual-wins rule above, one committed transaction.
- `process_schedule_payload(db, data, source_url=...) -> SyncLog` — the relay
  ingest path: many upserts (via the non-committing `_apply_schedule`) batched
  into one shared transaction and one `SyncLog` row, mirroring
  `sync_service.process_issues_payload()`'s pattern exactly. A `null`/missing
  `drawers_scheduled` for a date (the brief had no plan snapshot for it) is
  skipped and counted, never written as `0`.

## API

| Route | Auth | Purpose |
|---|---|---|
| `POST /api/v1/sync/daily-schedule/ingest-raw` | `X-Relay-Key` (reuses `RELAY_API_KEY` — no new secret) | Relay posts scraped `{date, drawers_scheduled}` pairs; upserts with `source="sync"` |
| `GET /api/v1/daily-production/schedule` | login session | `?date=` for one day, or `?start_date=&end_date=` for a range. A date with no row is simply absent from `schedules` — never a `0`. |
| `PUT /api/v1/daily-production/schedule` | login session | Manual entry/override; always `source="manual"`; writes an `audit_log` row |
| `GET /api/v1/daily-production/schedule-attainment` | login session | `{days, total_scheduled, total_inspected, attainment_pct}` for a date range — backs the Dashboard's Scheduled vs Completed card |
| `GET /api/v1/reports/date-preset` | login session | Resolves a Dashboard preset button name to `{start_date, end_date}` — see below |

`ingest-raw` is exempted from the login-required middleware the same one exact
way `/customer-issues/ingest-raw` already is (`app/auth_middleware.py
PUBLIC_EXACT_PATHS`) — an unattended script can't hold a browser session, so it's
gated by the relay key instead, at the same security bar.

Route registration note: `/schedule` and `/schedule-attainment` are registered in
`app/routers/daily_production.py` **before** the dynamic `/{production_date}`
routes, because Starlette matches routes in registration order and an untyped
single path segment would otherwise swallow a literal `/schedule` request before
FastAPI's date parsing ever gets a chance to reject it as one exact path.

## Relay (`scripts/relay_customer_issues.py`)

The existing hourly relay now does two independent fetch+forward passes per run:

1. Customer issues (unchanged since Phase 3).
2. **Daily schedule (new):** scrape `drawers.html` (today) plus a trailing
   7-day window of `/archive/<date>/drawers.html` (self-heals a missed hourly
   run), forward the scraped `{date, drawers_scheduled}` map to
   `.../daily-schedule/ingest-raw`. The HTML scrape is isolated to one function,
   `_scrape_drawers_scheduled()` — swap that one function out if/when a real API
   ever ships; nothing else changes.

The two passes never block each other: a failure in one (production brief down,
Render down, bad response) is logged and does not prevent the other from running.
`run()`'s overall exit code is `0` only if both succeeded.

The brief's own number is a first-write-wins snapshot taken once at its ~06:15 ET
daily generation (confirmed via `state.upsert_daily_first` in the production-brief
repo) — re-scraping it hourly is therefore harmless; it's the same number every
time after the morning run, satisfying "overwrite today's value on every hourly
run" without any dedup logic needed on this side.

## Daily Production Summary form

**Drawers Scheduled** sits beside **Drawers Inspected** — pre-filled from
`daily_schedules` for the selected date, editable, blank/unknown renders as `—`
(never `0`). A small line under the field distinguishes "from production brief"
(synced) from "entered manually." Saving the form only PUTs a schedule change
(`source="manual"`) if the value in the field actually differs from what was
loaded — submitting the rest of the form untouched must never silently flip an
untouched synced value to `"manual"` and pin it against future syncs.

## Dashboard

**Date presets** (Today / Yesterday / Last 7 days / Last 30 days / Month to
date / Custom range) control the *entire* dashboard, same date-range state as
before — just extended, not parallel. Every boundary resolves in
`DISPLAY_TIMEZONE` (`America/New_York`), inclusive, computed **server-side**
(`app/timezone_utils.py resolve_date_preset()`, called via
`GET /api/v1/reports/date-preset`) rather than re-derived in JavaScript — one
implementation to get right and unit-test, not two that can drift. Storage stays
UTC, unchanged.

> **DECISION FLAG (unresolved as of this writing):** Rodolfo said "last week,"
> "last month," and "up to today" when describing what he wanted. This addendum
> implements those as trailing 7 days / trailing 30 days / month-to-date. If he
> meant *previous calendar* week/month, or year-to-date, that's a small change —
> confirm before relying on the "Last 7 Days"/"Last 30 Days" labels meaning
> exactly that.

**Scheduled vs Completed card:** grouped bar chart (Scheduled muted, Completed
primary), one pair per day in the selected range, plus a **Schedule Attainment %**
tile (`total_inspected / total_scheduled * 100`). `None`/`N/A` when
`total_scheduled` is `0` or unknown — same convention `compute_kpis()` already
uses for `drawers_inspected == 0`. A day with a schedule but no summary row shows
a real Scheduled bar against a zero Completed bar — that gap is the point.
Single-day ranges (Today/Yesterday) render as one pair plus the tile.

## Reports + exports

- Reports' Trend section gained a Scheduled/Completed/Attainment table under the
  existing chart, sourced from the same `/reports/trend` response
  (`TrendPointOut.drawers_scheduled` / `.schedule_attainment_pct`, bucketed the
  same way as every other trend series).
- Reports' Summary card gained "Drawers scheduled" / "Schedule attainment" tiles,
  computed from the date range alone (schedule attainment ignores the
  work-order/category/station/etc. defect filters — it's a whole-day figure, not
  a defect-item rollup).
- The defect CSV export gained `day_drawers_scheduled` / `day_schedule_attainment_pct`
  columns, joined by `production_date` exactly like the existing Phase 4
  `day_cost_per_drawer` / `day_internal_rework_cost` columns — blank (not `0`)
  for a date with no `daily_schedules` row.

## Testing

`tests/unit/test_schedule_service.py` (upsert rules, range queries, relay ingest
processing), `tests/unit/test_schedule_attainment.py` (attainment math incl. the
`0`/unknown → `N/A` paths and the two-shift-day sum), `tests/unit/test_date_presets.py`
(every preset boundary, with `resolve_date_preset`'s injectable `now` used to pin
a moment past a UTC-date-boundary hour so a naive UTC-based implementation would
fail), `tests/api/test_schedule_api.py` (the login-gated routes),
`tests/api/test_schedule_sync_api.py` (the relay ingest route — key auth,
malformed payloads, manual-wins, mirroring `test_sync_ingest_api.py`'s structure).
