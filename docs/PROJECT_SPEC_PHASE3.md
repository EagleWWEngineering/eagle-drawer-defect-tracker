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
app. `since` defaults to the last successful sync's completion date minus a
`SINCE_LOOKBACK_BUFFER_DAYS` (3-day) buffer, or 90 days back on the very first run.
The buffer exists because the brief's classifier can take up to ~1 day to add an
email to `/api/quality-issues`, tagged with the day it was *received* rather than
classified — without a buffer, an empty-result sync that completes after midnight
would advance `since` past that day and permanently hide the issue once it does
appear. Re-syncing the buffered window is safe: issues are upserted by
`source_thread_id`, so re-fetching a day already synced just re-applies the brief-
sourced field refresh described below, never a duplicate.

**Historical note (superseded by "Relay follow-up" below):** the background task
(`app.main`'s `lifespan`, calling `sync_service.run_periodic_sync`) originally ran
one sync immediately on app startup, then every `SYNC_INTERVAL_MINUTES` (default
60), cancelled cleanly on shutdown. `sync_service.run_periodic_sync()`/`run_sync()`
themselves are unchanged and still work exactly as described above - only the
automatic scheduling of that task was retired, for the reason in the next section.

## Relay follow-up: Render can't reach the production brief directly

Render's servers cannot reach the production brief directly (confirmed firewalled
on the production brief's side). Every automatic run of the periodic background
task above always failed there, so `app.main`'s `lifespan` no longer schedules it.
Customer Issues sync instead happens exclusively via a local relay running on a
machine that CAN reach the production brief:

- **`scripts/relay_customer_issues.py`** (unchanged, still hourly): fetches the raw
  payload from the production brief and `POST`s it, unmodified, to
  `POST /api/v1/sync/customer-issues/ingest-raw`, which calls the exact same
  `process_issues_payload()` used by the direct-fetch path above - no mapping/dedup
  logic is duplicated.
- **`scripts/relay_poll.py`** (new, intended every ~1 minute): calls
  `GET /api/v1/sync/customer-issues/relay-status` (a cheap, DB-only heartbeat - no
  production-brief call happens there). That call itself updates
  `sync_relay_last_seen_at` (an `AppSetting`, see `app/services/sync_service.py`)
  and reports whether a manual sync is pending; if so, this script immediately runs
  the exact same fetch+ingest as `relay_customer_issues.py` by calling its `run()`
  function directly, never reimplementing that logic.

The Customer Issues tab's "Sync Now" button no longer triggers a direct fetch (that
always failed on Render). It calls `POST /api/v1/sync/customer-issues/request-
manual-sync` instead, which just sets another `AppSetting`
(`sync_manual_requested_at`) and returns instantly; the local relay's next
check-in notices it, runs a full relay pass, and the ingest endpoint clears the
flag on success. The manual "Sync Now" debug route (`POST
/api/v1/sync/customer-issues`, still a direct fetch) is left in place and working,
just no longer called by the UI - harmless for local-network use/debugging.

## API

`POST /api/v1/customer-issues` (unchanged) still works for manual entry —
`source_thread_id` stays `null`. `GET /api/v1/sync/status` (most recent attempt,
`null` if never run) and `GET /api/v1/sync/logs?limit=20` (the Admin screen's "last
20 sync runs" table) are unchanged. `POST /api/v1/sync/customer-issues` ("Sync Now"
direct-fetch debug route) still exists but is no longer called by the UI - see
above. New (relay follow-up): `POST /api/v1/sync/customer-issues/request-manual-
sync` (the UI's actual "Sync Now" call), `GET /api/v1/sync/customer-issues/relay-
status` (RELAY_API_KEY-authenticated heartbeat, called by `scripts/relay_poll.py`),
and `GET /api/v1/sync/customer-issues/relay-connection` (normal login session,
polled by the UI for its connected/disconnected status line).

## UI

Customer Issues tab: "Sync now" button (now just requests a manual sync and shows
an instant confirmation toast - see "Relay follow-up" above) + a heartbeat-based
status line computed from `sync_relay_last_seen_at`:
"🟢 Local relay connected — last checked in at [time]." when the heartbeat is
within the last ~2 minutes, otherwise "🔴 Local relay not connected — sync will
resume automatically once it's back online. Last seen: [time or "never"]." Also an
`auto`/`manual` badge next to each issue number so staff can tell synced rows from
manually-entered ones. Admin: a Sync Log table of the last 20 attempts (unchanged -
still reflects both the relay's hourly ingest-raw calls and any manual-sync-
triggered one, distinguished by the `relay:` source_url prefix).

## Seed data

`scripts/seed_customer_issues.py` now checks for any row with `source_thread_id`
set before seeding, and skips entirely if real synced data is already present — so
running it never mixes fictional demo issues into a database that has real synced
data.
