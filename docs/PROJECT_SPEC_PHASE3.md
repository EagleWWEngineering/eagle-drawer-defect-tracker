# Phase 3 Addendum — Production Brief Sync

Addendum to [`PROJECT_SPEC.md`](PROJECT_SPEC.md) and
[`PROJECT_SPEC_PHASE2.md`](PROJECT_SPEC_PHASE2.md). Everything in both still applies
unchanged. This document covers only what Phase 3 added: pulling real Customer
Issues from the Eagle production brief's JSON API instead of relying solely on demo
seed data.

## Source

```
GET {PRODUCTION_BRIEF_URL}/api/quality-issues?since={date}&include_ignored=false&limit=500
```

`PRODUCTION_BRIEF_URL` is fully environment-configurable (`app/config.py`); no code
anywhere hardcodes a specific address. It currently defaults to
`http://127.0.0.1:8094`, a **temporary local test instance** used during
development. The real production address (`http://20.62.194.32:8094`) — which, as
of Phase 3's initial build, served only a static HTML "daily briefs" site rather
than the `/api/quality-issues` JSON endpoint — will be set via `PRODUCTION_BRIEF_URL`
in the real `.env` file once that instance is confirmed ready. The sync code itself
never depends on which address is configured; it works identically against either.

## Deduplication

`CustomerIssue.source_thread_id` (nullable, unique) stores the brief's `thread_id`
and is the only dedup key. Rows with it set were created/updated by sync; rows with
it `null` were entered manually through the UI (phone/walk-in reports) and sync never
touches them.

## Field mapping

See `app/services/sync_service.py` for the authoritative mapping (subcategory →
category name, brief `category` → `source_type`, `cost_note` → `piece_count` via a
"`N pc`" regex, defaulting to 1 piece / `piece_count * $100` when not derivable).

## What sync preserves on update (local edits win)

When an already-synced issue (`source_thread_id` matches) comes back from the brief
again, brief-sourced fields (customer name, order number, category, cost, station,
description, photos) are refreshed, but:

- **`linked_defect_case_id` is never touched by sync** — only a human, via the UI's
  link action, sets or clears it.
- **`status` only moves forward from `Open`.** If staff already moved a case to
  `Linked` (or a previous run/UI action set `Ignored`), a later sync can never
  downgrade it back — even if the brief now says `ignored: true`. An untouched `Open`
  issue *can* still be auto-ignored by the brief.
- **Staff-written notes are preserved.** A `needs_review` flag from the brief is
  prepended to `notes` (once — not duplicated on repeat syncs), never overwriting
  what staff already typed.

## Sync tracking and scheduling

Every attempt (success or failure) writes one `SyncLog` row
(`app/services/sync_service.run_sync`). `run_sync()` never raises — an unreachable
or malformed response is recorded as `status="failed"` with a message in `errors`,
so neither the hourly background task nor the "Sync Now" button ever crashes the
app. `since` defaults to the last successful sync's completion date, or 90 days back
on the very first run.

The background task (`app.main`'s `lifespan`, calling
`sync_service.run_periodic_sync`) runs one sync immediately on app startup, then
every `SYNC_INTERVAL_MINUTES` (default 60), and is cancelled cleanly on shutdown.

## API

`POST /api/v1/customer-issues` (unchanged) still works for manual entry —
`source_thread_id` stays `null`. New: `POST /api/v1/sync/customer-issues` ("Sync
Now"), `GET /api/v1/sync/status` (most recent attempt, `null` if never run), and
`GET /api/v1/sync/logs?limit=20` (added beyond the two routes in the original
request, since the Admin screen's "last 20 sync runs" table needed a list endpoint
distinct from the single-latest `/status`).

## UI

Customer Issues tab: "Sync now" button + a status line ("Last synced: 5 minutes
ago — 47 issues" / "Sync failed 2 hours ago — ..."), and an `auto`/`manual` badge
next to each issue number so staff can tell synced rows from manually-entered ones.
Admin: a Sync Log table of the last 20 attempts.

## Seed data

`scripts/seed_customer_issues.py` now checks for any row with `source_thread_id`
set before seeding, and skips entirely if real synced data is already present — so
running it never mixes fictional demo issues into a database that has real synced
data.
