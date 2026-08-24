# Eagle Drawer Defect Tracker — Technical Summary

Snapshot of the app's actual current state, verified directly against the codebase
(not recalled from memory or prior session summaries). Supersedes
`docs/TECHNICAL_SUMMARY_2026-08-17.md`, which predates Phase 6 and Phase 7 and is
now stale in every section below. Where a design changed more than once, only the
final, currently-running behavior is described in detail, with a brief note on
what was tried first and why it was replaced.

Verified by: reading `app/models.py`, `app/services/defect_service.py`,
`app/services/metrics_service.py`, `app/services/auth_service.py`,
`app/services/schedule_service.py`, `app/services/sync_service.py`, `app/schemas.py`,
`app/routers/sync.py`, `app/routers/daily_production.py`, `app/routers/reports.py`,
`app/auth_middleware.py`, `app/main.py`, `app/config.py`, `app/dependencies.py`,
`render.yaml`, `.env.example`, `pyproject.toml`, `mcp_server/server.py`, `CLAUDE.md`,
and the actual directory tree, plus running the real test suite and checking the
Alembic migration history and git log.

---

## 1. Architecture & stack

- **Language/framework:** Python ≥3.11, FastAPI 0.115.6, served by Uvicorn 0.34.0.
- **Database:** SQLite, accessed through SQLAlchemy 2.0.36 (declarative `Mapped[...]`
  style models). WAL journal mode, foreign keys ON, 15s busy timeout (`app/database.py`).
- **Migrations:** Alembic 1.14.0. **Nine** migration files currently exist (see §10)
  — three more than the prior summary's six: `daily_schedules` (Phase 6),
  `cost_per_drawer_at_time` on `defect_cases` (Phase 7), and the Phase 7 open-case
  status/disposition data migration.
- **Templating/frontend:** Server-rendered Jinja2 (3.1.5) HTML pages, plain
  vanilla JS/CSS — no frontend framework/bundler.
- **Other real dependencies:** `httpx` (outbound HTTP — production-brief sync,
  relay scripts), `mcp` 1.28.1 (the MCP server), `bcrypt` 4.2.1 (login password
  hashing), `python-dotenv` (loads `.env` locally).
- **Layering:** UI page routes and JSON API routers (`app/routers/*.py`, HTTP
  input/output only) → service layer (`app/services/*.py`, all business rules and
  DB queries) → SQLAlchemy models (`app/models.py`, persistence only, no rules).
  Routers never contain business logic; services never touch `Request`/`Response`
  objects. This is enforced by convention/code review, not by a technical barrier.
- **MCP server** (`mcp_server/server.py`): a separate stdio process (started by
  Claude Code/Codex, not by the web app) that exposes the same functionality as
  MCP tools. It calls the REST API over HTTP (`DEFECT_API_URL`) exactly like the
  browser UI does — it never touches the SQLite file directly. This is what
  guarantees the UI and MCP path can never apply different business rules to the
  same write.
- **Hosting (Render):** one `web` service, `plan: starter` (persistent disks
  require a paid plan — the free tier doesn't support them). Build:
  `pip install .` (this repo has no `requirements.txt`; it uses `pyproject.toml`).
  Start: `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
  A 1GB persistent disk is mounted at `/var/data`, holding:
  - `/var/data/defect_tracker.db` — the live SQLite database (`DATABASE_URL`)
  - `/var/data/uploads/` — uploaded defect photos (`UPLOADS_DIR`)

---

## 2. Data model

All tables currently defined in `app/models.py` (13 total):

| Table | Purpose | Key fields |
|---|---|---|
| `stations` | Production steps (e.g. "Dado", "QC / Sorting / Shipping") | `name` (unique), `active`, `sort_order` |
| `defect_categories` | Internal defect classifications | same shape as `stations` |
| `customer_issue_categories` | Customer-complaint classifications (deliberately separate vocabulary from `defect_categories`) | same shape as `stations` |
| `daily_production_summaries` | One row per (`production_date`, `shift`) — the denominators for every rate | `drawers_inspected`, `drawers_rejected_unique`, `drawers_reworked`, `drawers_scrapped` (both kept for backward compatibility — neither is on the form anymore, see §3), `cost_per_drawer_at_time` (nullable historical rate snapshot, no longer read for cost — see §4). Unique on (`production_date`, `shift`). |
| **`daily_schedules`** *(new, Phase 6)* | One row per calendar date: how many drawers the production brief scheduled to finish that day — the denominator for Schedule Attainment % | `production_date` (**primary key**, not a surrogate id — deliberately one row per day, no shift dimension), `drawers_scheduled`, `source` (`"sync"` or `"manual"`, check-constrained), `synced_at` (null if never synced), `updated_at`. Manual-wins: a `source="manual"` row is never overwritten by a later sync (§6). |
| `defect_cases` | One QC finding for one work order — the header record | `case_number` (unique, `DF-YYYYMMDD-NNNN`), `found_station_id`/`possible_source_station_id` (FKs to `stations`), `priority`, `status`, `disposition`, `resolved_on_the_spot` (bool), `skipped_recheck` (bool, **retired** — see §3), `cost_per_drawer_at_time` *(new, Phase 7)* — nullable rate snapshot at creation, `closed_at`, `is_deleted` (soft delete) |
| `defect_items` | One category on one case, with an affected-drawer quantity | FK to `defect_cases` + `defect_categories`; unique on that pair (duplicates are merged, never a second row) |
| `defect_photos` | Metadata for a photo attached to a case (file lives on disk at `uploads_dir/stored_filename`) | `stored_filename`, `original_filename`, `content_type` |
| `status_history` | Audit trail of every status change on a case | `from_status`, `to_status`, `note`, `changed_at` |
| `audit_log` | Append-only log of every create/edit/status-change/delete/export/MCP-write | `actor_role`, `action`, `entity_type`, before/after JSON |
| `customer_issues` | A customer complaint, synced from the production brief or entered manually | `issue_number` (unique), `source_thread_id` (unique, nullable — the sync dedup key; null = manual entry, never touched by sync), `linked_defect_case_id` (optional FK to `defect_cases`), `status` (Open/Ignored/Linked) |
| `sync_logs` | One row per production-brief sync attempt (Customer Issues **or** daily-schedule, relay-ingested or direct), success or failure | `source_url`, `records_fetched/created/updated/skipped`, `errors`, `status` |
| `auth_sessions` | A server-side login session — no `expires_at`/TTL column at all | `token` (unique, opaque), `created_at` (informational only) |
| `app_settings` | Generic key/value store, intentionally schema-less so new singleton settings never need their own migration | `key` (primary key), `value` (string), `updated_at` |

No table and no column has ever been dropped across Phase 6 or Phase 7 — both were
implemented as additive schema changes plus one narrow, logged, non-destructive data
migration of currently-open cases only (see §3). `daily_production_summaries.drawers_reworked`/
`drawers_scrapped`/`cost_per_drawer_at_time` and `defect_cases.skipped_recheck` all
still exist with their historical values fully intact; nothing new writes to the
first three for cost purposes and nothing writes to `skipped_recheck` at all anymore.

**`app_settings` rows actually in use today:** `auth_username` / `auth_password_hash`
(§5), `cost_per_drawer` (§4), `sync_manual_requested_at` / `sync_relay_last_seen_at`
(§6, Customer Issues relay only — the daily-schedule relay in §6 has no pending/heartbeat
flags of its own, it's a plain unconditional forward).

**Relationships:** `defect_cases` → `defect_items`/`defect_photos`/`status_history`
(cascade delete-orphan); `defect_items` → `defect_categories`; `customer_issues` →
`customer_issue_categories` and optionally → `defect_cases`. `daily_schedules` has no
foreign keys — it's keyed purely by `production_date`, joined to
`daily_production_summaries` only in application code (`app/services/metrics_service.py`
`build_schedule_vs_completed`), never via a DB-level relationship, since the two
tables have different grains (whole-day vs. per-shift).

---

## 3. Core business rules (current, final state — Phase 7)

**Status and disposition vocabularies were simplified in Phase 7.** New writes are
restricted to a small fixed set; every value ever stored historically remains valid,
displayable, and filterable forever — this was implemented as a vocabulary/display
change, not a data rewrite (see the migration note below).

```python
# app/services/defect_service.py
VALID_STATUSES = ["Open", "Closed - Repaired", "Closed - Use As Is"]
RETIRED_STATUSES = ["In Rework", "Waiting", "Ready for QC Recheck", "Closed - Scrapped"]
ALL_KNOWN_STATUSES = VALID_STATUSES + RETIRED_STATUSES   # display/filter only

VALID_DISPOSITIONS = ["Rework", "Set Aside"]
RETIRED_DISPOSITIONS = ["Use As Is", "Hold", "Scrap"]
ALL_KNOWN_DISPOSITIONS = VALID_DISPOSITIONS + RETIRED_DISPOSITIONS   # display/filter only
```

`ALL_KNOWN_STATUSES`/`ALL_KNOWN_DISPOSITIONS` back the Reports/Dashboard filter
dropdowns (via `MasterDataOut.all_statuses`/`all_dispositions`) so a historical case
sitting in a retired value can still be filtered to; `VALID_STATUSES`/`VALID_DISPOSITIONS`
are the only values `create_defect_case`/`update_case_status` will ever accept for a
**new** write — this split is the load-bearing rule of Phase 7 and is never conflated
in either direction.

**Status transition map is now empty by design.** `STATUS_TRANSITIONS` maps every
status (including the retired ones, defensively) to an empty set — there is no more
generic "move to an intermediate open status" transition at all (no Waiting/In
Rework/Ready for QC Recheck to move into). The only two kinds of transition that
exist now:

- **Direct close** (`direct_close_statuses()`): every non-closed status — `Open` and,
  defensively, the retired `In Rework`/`Waiting`/`Ready for QC Recheck` in case one
  somehow still exists — can close straight to either `Closed - Repaired` or
  `Closed - Use As Is` (`NEW_CLOSE_STATUSES`), with an **optional** note. This is the
  standard, only way to close a case now. The retired `Closed - Scrapped` is
  deliberately excluded as a *new* target — nothing writes a case into it anymore,
  though a case already sitting there from before Phase 7 is untouched and still
  reopenable.
- **Reopen**: a closed case (any of the three closed statuses, including the retired
  `Closed - Scrapped`, per `CLOSED_STATUSES`) can move back to `Open`. This is the one
  transition that still always **requires** a note — the single audit-worthy exception
  left in the whole map.

**Dispositions:** `Rework` (default/primary — pre-selected, most prominent on the New
Defect form) and `Set Aside` (everything that used to be Use As Is/Hold/Scrap — "not
being worked right now," full stop). List order still drives the form's button
prominence, same discipline as before Phase 7.

**"Fixed immediately?" fast path** (`create_defect_case`): unchanged in spirit, but
narrower. Only `disposition="Rework"` has an instant-close path at all now (`Set Aside`
means "waiting to be worked" — the opposite of "already done"), and it requires a
`repair_action`. New in Phase 7: `instant_close_outcome` lets the form pick which
terminal status the instant-close lands in — `"Repaired"` (default,
`DEFAULT_INSTANT_CLOSE_OUTCOME`) or `"Use As Is"` — via `INSTANT_CLOSE_OUTCOMES =
{"Repaired": "Closed - Repaired", "Use As Is": "Closed - Use As Is"}`. This is how
"defect exists, we're shipping it as is" is recorded now that Use As Is is no longer
its own disposition — a decision Rodolfo confirmed keeping (see §8).

**Non-instant-close path:** every non-instant-close case now lands on `Open`,
**regardless of disposition** — there is no more separate In Rework/Waiting queue
status to route into. The `disposition` column still records *why* a case is open
(Rework in progress vs. Set Aside waiting); it just no longer picks a different
status for it. `skipped_recheck` is retired — the column and its historical
True/False values stay on old rows, but nothing writes to it for new status changes.

**The Phase 7 migration** (`7c1f9a2b4e6d_migrate_open_legacy_status_disposition.py`,
current Alembic head): moves every **currently-open** case sitting in a retired
status (`In Rework`, `Waiting`, `Ready for QC Recheck`) to `Open`, and every
currently-open case with a retired disposition (`Use As Is`, `Hold`, `Scrap`) to
`Set Aside` — logged as `StatusHistory` + `AuditLog` rows for each row it touches.
**Never touches an already-closed case's stored status/disposition, whatever it is**
— a case that closed `Closed - Scrapped` or with disposition `Use As Is` two months
ago still shows exactly that today. Verified against the real local dev DB before
deploy: 2 rows migrated; verified live on Render immediately after: 124 rows, all
`Closed - Repaired`, unchanged — 0 migrated, as expected since none were open.

**Cost model — completely rewritten (see §4 for the formulas).** Cost is now
snapshotted **per `DefectCase`** at creation (`cost_per_drawer_at_time`), not derived
from `DailyProductionSummary` at all. The old dual-source model (daily-summary sum,
falling back to a defect-case-derived estimate for dates with no summary row) is
gone entirely — there is exactly one source now, and it is always the defect cases.

**Rework Rate — redefined** (see §4): now the count of distinct cases with
disposition `"Rework"` in the filtered range, full stop, no status qualifier — not a
sum of the (now largely vestigial) `DailyProductionSummary.drawers_reworked` field.
A read-only, case-derived **"Reworked (from cases)"** column was added to the Daily
Summary page's Recent Entries table (`DailyProductionSummaryOut.reworked_case_count`,
via `defect_service.count_rework_cases_by_date()`) so that number stays visible now
that there's no editable field showing it — this was an explicit decision after
weighing "put `drawers_reworked` back on the form" against "leave it off, but surface
the real number somewhere read-only"; the latter won, to avoid a second,
possibly-disagreeing source of truth for the same fact.

**"% Queued Rework Closed Without Recheck" was removed entirely** — it depended on
`skipped_recheck`, which no longer means anything now that there's no recheck status
to skip past.

**Everything below is unchanged from before Phase 6/7:**

**Defect event counting:** one category logged on one case = one defect event,
regardless of physical defect count within that category; `affected_drawer_quantity`
on `DefectItem` is how multiple drawers under the same category on one case are
counted. Two categories on one case = two defect events but still one defective
drawer (one `DefectCase` = one defective drawer, however many `DefectItem` rows are
on it — this is also the exact dedup rule `suggested_daily_counts()` and the report
filters use). Duplicate categories submitted together on one case are merged
(quantities summed) rather than creating a second row, enforced by both
`_merge_duplicate_items()` and a DB unique constraint on (`defect_case_id`,
`defect_category_id`).

**Scrap removal (Phase 4, still in effect):** no Scrap Rate/Internal Scrap Cost
anywhere in this app; `drawers_scrapped` stays in the schema for historical/MCP
compatibility but drives no current KPI.

---

## 4. KPI formulas (current, verified against `app/services/metrics_service.py`)

All from `compute_kpis()`, `None` (displayed "N/A") whenever `drawers_inspected == 0`:

| KPI | Formula |
|---|---|
| Defects per 100 Drawers | `(defect_events / drawers_inspected) * 100` |
| Drawer Rejection Rate | `(unique_drawers_rejected / drawers_inspected) * 100` |
| First Pass Yield | `((drawers_inspected - unique_drawers_rejected) / drawers_inspected) * 100` |
| **Rework Rate** *(Phase 7, redefined)* | `(rework_case_count / drawers_inspected) * 100`, where `rework_case_count` = count of distinct, non-deleted `DefectCase` rows with `disposition == "Rework"` in the filtered range — **no status qualifier** (open or closed both count), and no longer any read of `DailyProductionSummary.drawers_reworked` |
| Total Internal Quality Cost | `= internal_rework_cost` (unchanged formula shape; the cost model feeding it is entirely new — see below) |
| Quality Cost per Drawer Inspected | `total_internal_quality_cost / drawers_inspected` |
| **Cost Avoided** *(Phase 7, new)* | sum, across the filtered cases, of what each case closed `"Closed - Use As Is"` *would have* cost had it not shipped as-is — the flip side of the cost formula below, making "shipping as-is instead of reworking" visible as a saving |

**Internal Quality Cost model — rewritten entirely in Phase 7**
(`compute_internal_quality_cost`, `compute_case_cost`, `compute_case_cost_avoided`).
One cost unit per `DefectCase` in the filtered range, using that case's own
snapshotted `cost_per_drawer_at_time` (or the currently-configured rate as a fallback,
for a case created before this column existed):

- Any case **not** closed `"Closed - Use As Is"` contributes its cost unit to
  **Internal Rework Cost** and zero to Cost Avoided.
- A case closed `"Closed - Use As Is"` contributes **zero** to Internal Rework Cost
  and its cost unit to **Cost Avoided** instead.
- **Never** multiplied by `affected_drawer_quantity` or `DefectItem` count — a case is
  one defective drawer no matter how many categories/items are on it.

This fully replaces the old Phase 4 dual-source model (a daily-summary-derived sum
for dates with a summary row, falling back to a defect-case-derived estimate for
dates without one) — there is exactly one source of cost now, and it is always the
defect cases, never `DailyProductionSummary`.

**Schedule Attainment % — new, Phase 6** (`compute_schedule_attainment_pct`,
`build_schedule_vs_completed`): `(total_inspected / total_scheduled) * 100` over a
date range, where `total_scheduled` is the sum of only the days that actually have a
`daily_schedules` row (a day with no row is excluded from the sum, never treated as a
real scheduled-zero). Returns `None`/"N/A" when not a single day in the range has a
known schedule, exactly mirroring how `compute_kpis` already treats
`drawers_inspected == 0`. Backs both the Dashboard's Scheduled vs Completed card
(one bar-pair per calendar day, `drawers_scheduled: null` rendered as "—", never `0`)
and a per-bucket `drawers_scheduled`/`schedule_attainment_pct` on the Reports Trend
endpoint.

**Unchanged since before Phase 6/7:**
- **% Resolved On The Spot** (`compute_resolved_on_the_spot_rate`):
  `resolved_on_the_spot_count / total_cases * 100` (of the filtered case set).
- There is still no scrap rate/cost anywhere in this app (Phase 4 "Scrap removal").

---

## 5. Authentication system

**Unchanged since Phase 2/5** — Phase 6 and 7 touched no auth code. Single shared
login for the entire app — no per-user accounts, no roles (the cosmetic "Role
(prototype)" header selector, `get_actor_role` in `app/dependencies.py`, is a separate
unauthenticated label used only to tag the audit log; it is not part of this login).

**Credential flow:** `APP_USERNAME`/`APP_PASSWORD_HASH` originate as environment
variables, but the value actually checked at login time lives in the `app_settings`
table (keys `auth_username`/`auth_password_hash`), kept in sync with the environment
on **every app startup** (`sync_credentials_from_env()`, called from
`seed_master_data()`) — not just once against an empty database, so a Render
dashboard credential change takes effect on the next redeploy/restart. Values are
cleaned (whitespace, then a matching pair of surrounding quotes) before being stored,
since a hosting dashboard stores env values completely literally with no
shell/dotenv-style parsing.

**Sessions** are rows in `auth_sessions`, deliberately with no `expires_at`/TTL
column — a session is valid for as long as its row exists, with no time-based expiry
check anywhere in the code (`session_age_seconds()` exists only as an informational
helper, never consulted to decide validity). This is what makes:
- **Normal "Log out"** possible: deletes only the one session row named by the
  current request's cookie; every other device stays logged in.
- **"Log out everywhere"** possible: deletes *every* `auth_sessions` row at once.

The cookie (`eagle_session`) is `HttpOnly`, deliberately **not** `Secure` (the app
runs over plain HTTP without a reverse-proxy TLS layer in front of it on the LAN
scenario — `Secure` would stop the browser from ever sending it back), with a
~10-year `max_age`. The long cookie lifetime is just what keeps the browser from
dropping it on its own; the actual "never expires" guarantee is that the server never
checks a session row's age.

Credential changes do **not** invalidate existing sessions — only "Log out
everywhere" does that (`sync_credentials_from_env()` never touches `auth_sessions`).

---

## 6. Customer Issues & schedule sync architecture

**Why it looks like this:** a direct fetch from Render to the production brief
(`20.62.194.32:8094`) was tried first and confirmed blocked — a connection timeout
reproduced from Render's own shell, consistent with a firewall on the production
brief's side that only allows the office network. Since Render itself can never
complete that fetch, all real syncing happens via a local relay running on a machine
that *can* reach the production brief. **Two independent data types now flow through
this same relay pattern:**

### Customer Issues (Phase 3, unchanged this session)

1. **Hourly automatic relay** (`scripts/relay_customer_issues.py`, Windows Task
   Scheduler): fetches raw JSON from the production brief's `/api/quality-issues`,
   then `POST`s the raw body unmodified to `POST /api/v1/sync/customer-issues/ingest-raw`,
   with an `X-Relay-Key` header checked via constant-time comparison against
   `RELAY_API_KEY`. That endpoint calls `sync_service.process_issues_payload()`
   directly — the exact same field-mapping/category-matching/dedup-by-
   `source_thread_id`/upsert logic the original direct-fetch path uses.
2. **On-demand "Sync Now"**: `POST /api/v1/sync/customer-issues/request-manual-sync`
   just records a `sync_manual_requested_at` timestamp and returns instantly. A
   companion heartbeat, `GET /api/v1/sync/customer-issues/relay-status`
   (`RELAY_API_KEY`-gated, meant to be polled ~1/minute by `scripts/relay_poll.py`),
   both records `sync_relay_last_seen_at` and reports whether a manual sync is
   pending. The pending flag is cleared unconditionally by
   `process_issues_payload()` on the next successful sync via either path.
3. **UI status line**: `GET /api/v1/sync/customer-issues/relay-connection` reports
   connected/disconnected based on whether the heartbeat was seen within
   `RELAY_HEARTBEAT_STALE_AFTER` (2 minutes).

### Daily schedule (Phase 6, new)

`app/services/schedule_service.py` + `POST /api/v1/sync/daily-schedule/ingest-raw`
(`app/routers/sync.py`). The production brief has **no JSON API** for the scheduled
drawer count — this had to be discovered by scraping its HTML (see
`docs/PRODUCTION_BRIEF_SCHEDULE_SOURCE.md`). The relay's existing unconditional hourly
run now scrapes and forwards this second, independent payload alongside the
Customer Issues one, reusing the exact same `RELAY_API_KEY` — no new secret. Body
shape: `{"schedules": [{"date": "YYYY-MM-DD", "drawers_scheduled": <int|null>}, ...]}`;
a null/missing figure means the brief had no "Today's plan" fact for that date and is
skipped (not an error).

**Manual-wins rule** (`schedule_service._apply_schedule`/`upsert_schedule`): if a date
already has a `source="manual"` row, a `source="sync"` write for that same date is
skipped entirely — the human's value and its `"manual"` provenance are left
completely untouched. A `source="manual"` call (the Daily Summary form's own
`PUT /api/v1/daily-production/schedule`) always applies immediately, overwriting
whatever was there. Both write paths funnel through this one function, so the rule
lives in exactly one place. Logged the same way as Customer Issues sync — one
`SyncLog` row per relay pass, with per-date skips/errors counted inside that row
rather than aborting the batch.

**Current gap, carried forward from the prior summary — still true, not re-verified
live this session since it's unrelated to Phase 6/7's changes:** the `relay_poll.py`
heartbeat task is not currently registered in Task Scheduler (removed by choice
earlier); only the hourly `EagleDefectTracker-RelaySync` task runs, so "Sync Now"
and the schedule relay are both effectively hourly-only right now, not near-instant.

`app.main`'s `lifespan` still does not schedule `sync_service.run_periodic_sync()` —
Render cannot reach the production brief directly, so that automatic task remains
retired; the code paths are kept for a future local-network deployment or manual
debugging.

---

## 7. Deployment details

**Environment variables** (source: `app/config.py`, `.env.example`, `render.yaml`):

| Variable | Purpose | Notes |
|---|---|---|
| `DATABASE_URL` | SQLite connection string | On Render: `sqlite:////var/data/defect_tracker.db` (persistent disk) |
| `UPLOADS_DIR` | Where uploaded defect photos are written/served from | On Render: `/var/data/uploads` (persistent disk) — see the 2026-08-17 incident (unchanged, documented in `app/main.py`'s startup warning) |
| `APP_HOST` / `APP_PORT` | Local dev-only uvicorn bind settings | Unused on Render — the start command's `--host 0.0.0.0 --port $PORT` overrides this directly |
| `DISPLAY_TIMEZONE` | Timezone for displayed timestamps (storage is always UTC) | Default `America/New_York` |
| `MAX_UPLOAD_MB` | Photo upload size limit | Default 8 |
| `DEFECT_API_URL` | Where the MCP server reaches the REST API | Not used by the web app itself |
| `PRODUCTION_BRIEF_URL` | Base URL of the Eagle production brief (used by both the Customer Issues and daily-schedule sync paths) | Real value: `http://20.62.194.32:8094` — a Render secret (`sync: false`) since it's internal infra |
| `SYNC_INTERVAL_MINUTES` | Interval `run_periodic_sync()` would use if ever re-enabled | Not currently driving anything automatic on Render (see §6) |
| `DEFAULT_COST_PER_DRAWER` | One-time seed value for the `app_settings` `cost_per_drawer` row | `render.yaml` marks it `sync: false` (secret prompt); has no effect after first startup — the Admin screen is authoritative afterward |
| `APP_USERNAME` / `APP_PASSWORD_HASH` | The single shared login credential | Secrets (`sync: false`); synced into `app_settings` on every startup (§5) |
| `RELAY_API_KEY` | Shared secret the relay scripts send via `X-Relay-Key` — now covers **both** the Customer Issues ingest and the Phase 6 daily-schedule ingest | Secret (`sync: false`); independent of the human login |
| `RENDER_URL` | The live app's own base URL | Only read by the local relay scripts, not by the FastAPI app itself |
| `PYTHON_VERSION` | Pins the Render Python runtime | `3.11.10` |

**`render.yaml`:** one `web` service (`env: python`, `plan: starter`), build
`pip install .`, start `alembic upgrade head && uvicorn app.main:app --host
0.0.0.0 --port $PORT`, one 1GB disk mounted at `/var/data`. Non-secret vars get real
default `value:`s in the file; secrets are declared `sync: false` so Render prompts
for them in its dashboard rather than storing them in git.

**Backups:** `scripts/backup_database.py` — used before both the Phase 6 schema
migration and the Phase 7 data migration, run via Render's dashboard Shell tab
(this sandbox has no Render API/CLI access and no plaintext login credential, so any
live-only verification step — backups included — is necessarily done by Rodolfo, not
by an agent working in this repo). Both migrations were confirmed against a restored
backup readback before the corresponding deploy.

**Known gotchas from before this session (unchanged, still relevant):**
- **`UPLOADS_DIR` persistent-disk incident** — a plain `value:` in `render.yaml` is
  only read when a service is first created via Render's Blueprint flow, not
  retroactively applied to an already-existing service; had to be set directly in
  Render's dashboard. `app/main.py` logs a startup warning if `UPLOADS_DIR` or
  `DATABASE_URL` isn't actually set in the environment.
- **Login credential not updating after a Render dashboard change** — led to the
  credential-storage redesign described in §5 (stored copy in `app_settings`, with
  whitespace/quote stripping).
- **Render cannot reach the production brief directly** — a network-level
  restriction on the production brief server's side (see §6, §8).

**Local machine scheduled tasks** (Windows Task Scheduler, supporting the relay):

| Task name | Schedule | What it does |
|---|---|---|
| `EagleDefectTracker-RelaySync` | Every 1 hour | Runs `scripts/relay_customer_issues.py` — unconditional fetch of **both** Customer Issues and the Phase 6 daily-schedule figures from the production brief, forwarded to their respective `ingest-raw` endpoints. **Currently the only registered task.** |
| `EagleDefectTracker-RelayPoll` | *(not currently registered)* | Would run `scripts/relay_poll.py` — the frequent heartbeat/on-demand-sync check-in described in §6. The script and server-side support still exist; only the schedule was removed. |

---

## 8. Known limitations / future improvement candidates

- **The root network restriction between Render and the production brief is still
  unresolved** — the local relay (now carrying two independent payload types, see
  §6) is a working workaround, not a fix. If that server is ever opened to Render's
  IP ranges, the original direct-fetch code (`run_sync`/`run_periodic_sync`) is
  still in place and could be re-enabled.
- **"Sync Now" is still slower than designed** (see §6) — the heartbeat poller that
  would make it near-instant isn't currently scheduled; both sync types are
  effectively hourly-only right now.
- **Uploaded photos are not covered by any backup process** — `scripts/backup_database.py`
  covers the SQLite file only.
- **No per-user accounts** — by design (single shared login); flagged in
  `app/dependencies.py`'s own docstring as something to replace "before any LAN or
  multi-user deployment" beyond this pilot's scope.
- **The Daily Summary auto-suggestion and the "Reworked (from cases)" column have no
  shift awareness** — `DefectCase` has no `shift` field, so both
  `suggested_daily_counts()` and `count_rework_cases_by_date()` compute at the
  whole-day level; a two-shift day gets identical figures on both shifts' forms/rows
  (documented directly in both functions' own docstrings).
- **A defect case created before the Phase 7 `cost_per_drawer_at_time` column existed
  has no cost snapshot** — `compute_case_cost`/`compute_case_cost_avoided` fall back
  to the currently-configured admin rate for those rows, which is the best available
  estimate but not a true historical snapshot (same caveat `DailyProductionSummary.cost_per_drawer_at_time`
  already had pre-Phase-7).
- **`daily_schedules` has no shift dimension by design** (see §2) — this is a
  deliberate modeling choice (the production brief's schedule is a whole-day figure),
  not a gap, but it does mean Schedule Attainment % can never be broken out by shift.
- **`scripts/backup_database.py` writes to an ephemeral path on Render, not the
  persistent disk.** Verified directly in the script: `backups_dir = PROJECT_ROOT /
  "data" / "backups"`, where `PROJECT_ROOT` is the repo's own directory — on Render
  that resolves under the ephemeral container filesystem (e.g.
  `/opt/render/project/src/data/backups/`), which is wiped on every redeploy, **not**
  `/var/data` (the actual persistent disk mount — see §7). A backup taken by running
  this script on Render survives only until the next deploy unless it is manually
  copied to `/var/data/backups/` first. Both live backups taken so far (before the
  Phase 6 schema migration and the Phase 7 data migration) were copied to
  `/var/data/backups/` by hand for exactly this reason; the script itself has no
  option to target the persistent disk and doesn't warn that its default output
  location won't survive a deploy.
- **The scheduled-drawer-count sync depends on scraping the production brief's HTML**
  (see §6, `docs/PRODUCTION_BRIEF_SCHEDULE_SOURCE.md`) because that server has no JSON
  API for this figure, unlike Customer Issues' `/api/quality-issues`. This is a
  fragile dependency: any change Blake makes to that page's layout/markup can break
  the scrape silently — a malformed or empty scrape looks the same as "the brief had
  no plan for that date" (`drawers_scheduled: null`, skipped rather than erroring, by
  design — see `schedule_service.process_schedule_payload`), so a layout change could
  go unnoticed for a while rather than surfacing as a loud failure. The real fix is a
  proper API route on the production brief for this figure, which would remove the
  scraping dependency entirely; until then, this is a known standing risk of the
  Phase 6 design, not a bug.

---

## 9. Testing & tooling

- **432 tests pass**, verified by actually running `pytest` just now — not estimated
  or recalled (up from 361 at the time of the prior summary: Phase 6 added schedule
  service/attainment/date-preset/API coverage, Phase 7 added disposition/status/
  cost-model/migration coverage plus the read-only reworked-count column). Organized as:
  - `tests/unit/` — pure business-logic tests (counting, KPI math, status
    transitions, auth credential sync, sync payload processing, cost tracking,
    schedule service/attainment, date presets, the Phase 7 migration itself, config,
    etc.) with no HTTP layer involved.
  - `tests/api/` — FastAPI `TestClient`-based end-to-end tests per router/feature
    area (auth, defect cases, daily production, reports, exports, customer issues,
    sync/ingest, schedule + schedule sync, resolved-on-the-spot, health).
  - `tests/mcp/` — tests for the MCP server's tools.
- **Linting:** Ruff 0.8.4, configured in `pyproject.toml`
  (`select = ["E", "F", "I", "W", "B"]`, `ignore = ["B008"]`, line length 100,
  target Python 3.11). `ruff check .` and `ruff format --check .` both currently
  pass clean.
- Test isolation: an in-memory SQLite database per test (`StaticPool`), FastAPI
  dependency overrides for `get_db`, and a pre-authenticated session cookie attached
  to the shared `client` fixture so most tests can ignore auth entirely (the
  dedicated `tests/api/test_auth_api.py` uses its own unauthenticated fixture to test
  the login flow itself). The Phase 7 migration test
  (`tests/unit/test_phase7_migration.py`) additionally invokes real Alembic via
  subprocess (`python -m alembic`) against a throwaway SQLite file, since `alembic/env.py`
  reads `DATABASE_URL` from the process environment rather than accepting an
  in-memory session directly.

---

## 10. File/module structure

```
app/
  main.py                  FastAPI app instance, lifespan/startup, page routes, static/uploads mounts
  config.py                Settings (env vars), cached via @lru_cache
  database.py              SQLAlchemy engine/session setup, SQLite pragmas
  models.py                All ORM models (persistence only, no business rules) - 13 tables
  schemas.py               Pydantic request/response models
  seed_data.py             Baseline master data + credential sync, run on every startup
  auth_middleware.py       LoginRequiredMiddleware - gates every route except a small allowlist
  dependencies.py          Shared FastAPI dependencies (get_db re-export, get_actor_role)
  errors.py                Shared ServiceError hierarchy -> uniform JSON error envelope
  timezone_utils.py        Display-timezone conversion + date-preset resolution (Today/Yesterday/etc.)
  routers/
    auth.py                  Login / logout / logout-everywhere
    defect_cases.py          Create/list/edit/status/delete/photos for DefectCase
    daily_production.py     Daily Production Summary CRUD + suggested-counts + schedule CRUD/attainment (Phase 6)
    reports.py               KPI summary, Pareto, trend (incl. schedule/attainment per bucket), work-order history, rework queue, date-preset
    customer_issues.py       Customer Issues CRUD + CSV export
    sync.py                  Production-brief sync control + relay ingest/heartbeat for BOTH Customer Issues and daily schedule (see §6)
    master_data.py           Stations/categories/priorities/statuses/dispositions (valid + all-known)
    exports.py               Defect CSV export
    settings.py              Admin-editable app settings (cost_per_drawer)
  services/
    defect_service.py        Case numbering, creation, status transitions, cost snapshot at creation (see §3)
    metrics_service.py        KPI/Pareto/trend/sort/cost/schedule-attainment math (see §4)
    schedule_service.py       Daily schedule CRUD + manual-wins sync upsert + relay payload processing (Phase 6, see §6)
    customer_issue_service.py Customer Issue business rules
    sync_service.py           Customer Issues sync + relay heartbeat/pending-request (see §6)
    auth_service.py           Login/session/credential-sync logic (see §5)
    settings_service.py       Generic app_settings read/write helpers
    audit_service.py          Writes AuditLog rows
    export_service.py         CSV export generation
  templates/                 Server-rendered Jinja2 pages (one per app/main.py page route)
  static/                    Plain CSS/JS, no build step
alembic/versions/            Nine migrations, in order: initial schema -> customer-issue tables +
                              source_thread_id/sync_logs -> resolved_on_the_spot/skipped_recheck ->
                              app_settings/cost_per_drawer -> auth_sessions -> daily_schedules (Phase 6) ->
                              cost_per_drawer_at_time on defect_cases (Phase 7) ->
                              migrate open legacy status/disposition (Phase 7, head)
mcp_server/server.py         Stdio MCP server - calls the REST API, never touches SQLite directly
scripts/
  relay_customer_issues.py    Hourly full relay: fetches + forwards BOTH Customer Issues and daily-schedule figures
  relay_poll.py                Frequent heartbeat/on-demand-sync poller (see §6 for current status)
  backup_database.py           SQLite backup utility
  seed_demo_data.py / seed_customer_issues.py  Synthetic data for local development
tests/                       unit/, api/, mcp/ - see §9
docs/                        PROJECT_SPEC.md + phase addenda (through Phase 7), DATA_DICTIONARY.md,
                              PRODUCTION_BRIEF_SCHEDULE_SOURCE.md, setup/user guides
render.yaml                  Render Blueprint (see §7)
.env.example                 Documented template for every environment variable (see §7)
pyproject.toml                Dependencies, Ruff config, pytest config, build config
```
