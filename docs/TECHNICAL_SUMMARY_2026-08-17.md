# Eagle Drawer Defect Tracker — Technical Summary

Snapshot of the app's actual current state, verified directly against the codebase
(not recalled from memory or prior session summaries). Where a design changed more
than once, only the final, currently-running behavior is described in detail, with
a brief note on what was tried first and why it was replaced.

Verified by: reading `app/models.py`, `app/services/defect_service.py`,
`app/services/metrics_service.py`, `app/services/auth_service.py`,
`app/services/sync_service.py`, `app/routers/sync.py`, `app/auth_middleware.py`,
`app/main.py`, `app/config.py`, `app/dependencies.py`, `render.yaml`,
`.env.example`, `pyproject.toml`, `mcp_server/server.py`, and the actual directory
tree, plus running the real test suite and Ruff.

---

## 1. Architecture & stack

- **Language/framework:** Python ≥3.11, FastAPI 0.115.6, served by Uvicorn 0.34.0.
- **Database:** SQLite, accessed through SQLAlchemy 2.0.36 (declarative `Mapped[...]`
  style models). WAL journal mode, foreign keys ON, 15s busy timeout (`app/database.py`).
- **Migrations:** Alembic 1.14.0. Six migration files currently exist (see §10).
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

All tables currently defined in `app/models.py`:

| Table | Purpose | Key fields |
|---|---|---|
| `stations` | Production steps (e.g. "Dado", "QC / Sorting / Shipping") | `name` (unique), `active`, `sort_order` |
| `defect_categories` | Internal defect classifications | same shape as `stations` |
| `customer_issue_categories` | Customer-complaint classifications (deliberately separate vocabulary from `defect_categories`) | same shape as `stations` |
| `daily_production_summaries` | One row per (`production_date`, `shift`) — the denominators for every rate | `drawers_inspected`, `drawers_rejected_unique`, `drawers_reworked`, `drawers_scrapped` (kept for backward compatibility, no longer collected via the form), `cost_per_drawer_at_time` (nullable rate snapshot). Unique on (`production_date`, `shift`). |
| `defect_cases` | One QC finding for one work order — the header record | `case_number` (unique, `DF-YYYYMMDD-NNNN`), `found_station_id`/`possible_source_station_id` (FKs to `stations`), `priority`, `status`, `disposition`, `resolved_on_the_spot` (bool), `skipped_recheck` (bool), `closed_at`, `is_deleted` (soft delete) |
| `defect_items` | One category on one case, with an affected-drawer quantity | FK to `defect_cases` + `defect_categories`; unique on that pair (duplicates are merged, never a second row) |
| `defect_photos` | Metadata for a photo attached to a case (file lives on disk at `uploads_dir/stored_filename`) | `stored_filename`, `original_filename`, `content_type` |
| `status_history` | Audit trail of every status change on a case | `from_status`, `to_status`, `note`, `changed_at` |
| `audit_log` | Append-only log of every create/edit/status-change/delete/export/MCP-write | `actor_role`, `action`, `entity_type`, before/after JSON |
| `customer_issues` | A customer complaint, synced from the production brief or entered manually | `issue_number` (unique), `source_thread_id` (unique, nullable — the sync dedup key; null = manual entry, never touched by sync), `linked_defect_case_id` (optional FK to `defect_cases`), `status` (Open/Ignored/Linked) |
| `sync_logs` | One row per production-brief sync attempt (relay-ingested or direct), success or failure | `source_url`, `records_fetched/created/updated/skipped`, `errors`, `status` |
| `auth_sessions` | A server-side login session — no `expires_at`/TTL column at all | `token` (unique, opaque), `created_at` (informational only) |
| `app_settings` | Generic key/value store, intentionally schema-less so new singleton settings never need their own migration | `key` (primary key), `value` (string), `updated_at` |

**Relationships:** `defect_cases` → `defect_items`/`defect_photos`/`status_history`
(cascade delete-orphan); `defect_items` → `defect_categories`; `customer_issues` →
`customer_issue_categories` and optionally → `defect_cases`. `app_settings` has no
foreign keys — it's used as a schema-less singleton store for things that don't
warrant their own table (see §3–6 below for what's actually stored there today:
the login credential, the cost-per-drawer rate, and the relay sync's
heartbeat/pending-request flags).

---

## 3. Core business rules (current, final state)

**Status transition map** (`app/services/defect_service.py STATUS_TRANSITIONS`),
for transitions that do *not* close a case:

```
Open                  → In Rework, Waiting
In Rework             → Ready for QC Recheck, Waiting
Waiting               → In Rework
Ready for QC Recheck  → In Rework
Closed - Repaired     → (terminal)
Closed - Scrapped     → (terminal)
Closed - Use As Is    → (terminal)
```

Separately, **every** non-closed status (`Open`, `In Rework`, `Waiting`, and the
legacy `Ready for QC Recheck`) can close **directly** to any of the three closed
statuses (`direct_close_statuses()`) — this is the standard way to close a case
today, not a narrow exception. `Ready for QC Recheck` is kept only for backward
compatibility with historical data; nothing in the current UI presents moving a
case *into* it as an expected step, since there's no real "QC recheck" moment on
the floor. Reopening a closed case back to `Open` always requires a note (the one
transition still treated as audit-worthy); a normal direct closure's note is
optional.

**Dispositions** (`VALID_DISPOSITIONS`): `["Rework", "Use As Is", "Hold", "Scrap"]`
— that list order is deliberate and drives the New Defect form's button
prominence (Rework big and pre-selected; Use As Is/Hold secondary; **Scrap tucked
behind a "More options..." disclosure** — confirmed current in
`defect_service.py`'s own comment). Verified current removal of Scrap from
tracking/reporting:
- Daily Production Summary form has no scrapped field (confirmed no such field in
  `upsert_daily_summary`'s required inputs — `drawers_scrapped` is optional and,
  when omitted, preserves whatever was already stored rather than zeroing it).
- `compute_kpis()` in `metrics_service.py` computes `defects_per_100`,
  `rejection_rate`, `first_pass_yield`, `rework_rate` — **no Scrap Rate anywhere**
  in that function.
- `compute_internal_quality_cost()` computes rework cost only — its own docstring
  states scrap cost "was dropped from this app entirely."
- `dashboard.html`/`reports.html` contain no scrap KPI tiles (a single leftover
  code *comment* in `reports.html` documents that removal — no visible scrap UI).
- The `drawers_scrapped` column and `Scrap` disposition/status values are
  deliberately **kept** in the schema for backward compatibility with historical
  and MCP-written rows — this is a display/reporting removal, not a data-model
  removal.

**"Resolved on the spot" fast path** (`create_defect_case`): this is the New
Defect form's *default* flow, not an opt-in toggle. Choosing a disposition of
Rework, Scrap, or Use As Is (never Hold — Hold means the disposition itself isn't
decided yet) and providing a `repair_action` creates the case directly in its
terminal closed status (`Closed - Repaired`/`Closed - Scrapped`/`Closed - Use As
Is`), skipping Open/In Rework/Ready for QC Recheck entirely. A secondary "Not
resolved yet — leave open" checkbox opts *out* of this default. This is tracked
via `DefectCase.resolved_on_the_spot` (set once at creation, never changed).

**Queued/Hold path:** a case without `resolved_on_the_spot` starts at `Open`
(no disposition) or at the status implied by `DISPOSITION_TO_STATUS` if a
disposition is chosen without also resolving on the spot (`Rework`→`In Rework`,
`Hold`→`Waiting`, `Scrap`/`Use As Is`→`Open`) — it then moves through the Rework
Queue and closes later via `update_case_status`, tracked via
`DefectCase.skipped_recheck` (true when a case closes directly without having
passed through the legacy `Ready for QC Recheck` status first).

**Defect event counting:** one category logged on one case = one defect event,
regardless of physical defect count within that category; `affected_drawer_quantity`
on `DefectItem` is how multiple drawers under the same category on one case are
counted. Two categories on one case = two defect events but still one defective
drawer (one `DefectCase` = one defective drawer, however many `DefectItem` rows
are on it — this is also the exact dedup rule `suggested_daily_counts()` and the
report filters use). Duplicate categories submitted together on one case are
merged (quantities summed) rather than creating a second row, enforced by both
`_merge_duplicate_items()` and a DB unique constraint on
(`defect_case_id`, `defect_category_id`).

---

## 4. KPI formulas (current, verified against `app/services/metrics_service.py`)

All from `compute_kpis()`, `None` (displayed "N/A") whenever `drawers_inspected == 0`:

| KPI | Formula |
|---|---|
| Defects per 100 Drawers | `(defect_events / drawers_inspected) * 100` |
| Drawer Rejection Rate | `(unique_drawers_rejected / drawers_inspected) * 100` |
| First Pass Yield | `((drawers_inspected - unique_drawers_rejected) / drawers_inspected) * 100` |
| Rework Rate | `(drawers_reworked / drawers_inspected) * 100` |
| Total Internal Quality Cost | `= internal_rework_cost` (scrap cost is not part of this formula — confirmed, see §3) |
| Quality Cost per Drawer Inspected | `total_internal_quality_cost / drawers_inspected` |

**Internal rework cost** (`compute_internal_quality_cost`) is the higher of two
sources per production date, never double-counted:
- **Daily-summary-derived** (`sum_internal_rework_cost`): for any date that *has*
  a `DailyProductionSummary` row, `drawers_reworked * cost_per_drawer_at_time`
  (falling back to the currently-configured rate if that row predates rate
  snapshotting).
- **Defect-case-derived fallback** (`defect_case_derived_rework_count` /
  `classify_case_cost_bucket`): for any date with **no** summary row at all, one
  unit of rework cost per qualifying `DefectCase` (disposition Rework, status
  `Closed - Repaired`) at the *currently* configured rate — this exists so real
  cost isn't silently $0 just because nobody filled out a Daily Summary for that
  date.

Two other current KPIs, unrelated to `compute_kpis()`:
- **% Resolved On The Spot** (`compute_resolved_on_the_spot_rate`):
  `resolved_on_the_spot_count / total_cases * 100` (of the filtered case set).
- **% Queued Rework Closed Without Recheck** (`compute_skip_recheck_rate`):
  `skipped_recheck_count / queued_rework_count * 100` (only among cases that
  actually reached `In Rework`, i.e. excludes ones resolved on the spot).

**Confirmed genuinely removed this session:** Scrap Rate and Internal Scrap Cost
do not appear in `compute_kpis()`, `compute_internal_quality_cost()`,
`dashboard.html`, or `reports.html` — verified directly, not assumed.

---

## 5. Authentication system

Single shared login for the entire app — no per-user accounts, no roles (the
cosmetic "Role (prototype)" header selector, `get_actor_role` in
`app/dependencies.py`, is a separate unauthenticated label used only to tag the
audit log; it is not part of this login).

**Credential flow (post-incident design — see §7 for the incident):**
`APP_USERNAME`/`APP_PASSWORD_HASH` originate as environment variables, but the
value actually checked at login time lives in the `app_settings` table (keys
`auth_username`/`auth_password_hash`), kept in sync with the environment on
**every app startup** (`sync_credentials_from_env()`, called from
`seed_master_data()`) — not just once against an empty database, so a Render
dashboard credential change takes effect on the next redeploy/restart. Values are
cleaned (whitespace, then a matching pair of surrounding quotes) before being
stored, since a hosting dashboard stores env values completely literally with no
shell/dotenv-style parsing.

**Sessions** are rows in `auth_sessions`, deliberately with no `expires_at`/TTL
column — a session is valid for as long as its row exists, with no time-based
expiry check anywhere in the code. This is what makes:
- **Normal "Log out"** possible: deletes only the one session row named by the
  current request's cookie; every other device stays logged in.
- **"Log out everywhere"** possible: deletes *every* `auth_sessions` row at once,
  including the caller's own. Requires re-entering the current password in the
  request body first (checked via the same `verify_credentials` the login route
  uses) — a wrong password invalidates nothing.

The cookie (`eagle_session`) is `HttpOnly`, `SameSite=Lax`, deliberately **not**
`Secure` (the app runs over plain HTTP without a reverse-proxy TLS layer in front
of it on the LAN scenario — `Secure` would stop the browser from ever sending it
back), with a ~10-year `max_age`. The long cookie lifetime is just what keeps the
browser from dropping it on its own; the actual "never expires" guarantee is that
the server never checks a session row's age.

Credential changes do **not** invalidate existing sessions — only "Log out
everywhere" does that (verified: `sync_credentials_from_env()` never touches
`auth_sessions`).

---

## 6. Customer Issues sync architecture (final working design)

**Why it looks like this:** a direct fetch from Render to the production brief
(`20.62.194.32:8094`) was tried first and confirmed blocked — a connection
timeout reproduced from Render's own shell, consistent with a firewall on the
production brief's side that only allows the office network. Since Render itself
can never complete that fetch, all real syncing now happens via a local relay
running on a machine that *can* reach the production brief.

**Current flow, as implemented in `app/services/sync_service.py` and
`app/routers/sync.py`:**

1. **Hourly automatic relay** (`scripts/relay_customer_issues.py`, run via a
   Windows Task Scheduler entry on Rodolfo's machine — unconditional, runs every
   hour regardless of anything else): fetches raw JSON from the production
   brief's `/api/quality-issues`, then `POST`s that raw body, unmodified, to
   `POST /api/v1/sync/customer-issues/ingest-raw` on the live Render app, with a
   `X-Relay-Key` header checked via constant-time comparison against
   `RELAY_API_KEY` (a secret deliberately separate from the human login, so a
   password rotation never breaks the relay or vice versa). That endpoint calls
   `sync_service.process_issues_payload()` directly — the exact same
   field-mapping/category-matching/dedup-by-`source_thread_id`/upsert logic the
   original direct-fetch path uses (`fetch_issues()` → `process_issues_payload()`
   for a direct fetch; the relay path skips straight to `process_issues_payload()`
   since the fetch already happened locally). Nothing is duplicated between the
   two paths.
2. **On-demand "Sync Now"**: the Customer Issues tab's button calls
   `POST /api/v1/sync/customer-issues/request-manual-sync` (normal login session,
   no special auth) which just writes a `sync_manual_requested_at` timestamp into
   `app_settings` and returns instantly — no network call. A companion script,
   `scripts/relay_poll.py`, is designed to poll a cheap heartbeat endpoint
   (`GET /api/v1/sync/customer-issues/relay-status`, also `RELAY_API_KEY`-gated)
   roughly once a minute; that call itself *is* the heartbeat
   (`sync_relay_last_seen_at`), and its response tells the poller whether a
   manual sync is pending, in which case it performs the same full relay pass as
   #1. The pending flag is cleared unconditionally by `process_issues_payload()`
   on the next successful sync via *either* path.
3. **UI status line**: `GET /api/v1/sync/customer-issues/relay-connection`
   (normal login, read-only) reports connected/disconnected based on whether the
   heartbeat was seen within the last 2 minutes (`RELAY_HEARTBEAT_STALE_AFTER`).

**Current gap, verified live on this machine just now:** the `relay_poll.py`
Task Scheduler entry was deliberately removed earlier this session (a decision
Rodolfo made to reduce background chatter). Only the hourly
`EagleDefectTracker-RelaySync` task is currently registered. The
request-manual-sync/heartbeat *code path* is fully built and tested, but nothing
is currently polling it — so as things stand right now, clicking "Sync Now" sets
the pending flag but it will only actually be picked up by the next scheduled
hourly relay run, not within ~1 minute as the mechanism was originally designed
to support. Re-registering `relay_poll.py` (at whatever interval is wanted) would
restore near-instant "Sync Now" behavior without any code change.

`app.main`'s `lifespan` no longer schedules `sync_service.run_periodic_sync()` at
all (this was the original direct-fetch-from-Render automatic task — retired
since it always failed). `run_sync()`, `run_periodic_sync()`, and the manual
`POST /api/v1/sync/customer-issues` debug route are all still present in the code
(not deleted) for local-network deployment or manual debugging, but nothing calls
them automatically in production today.

---

## 7. Deployment details

**Environment variables** (source: `app/config.py`, `.env.example`, `render.yaml`):

| Variable | Purpose | Notes |
|---|---|---|
| `DATABASE_URL` | SQLite connection string | On Render: `sqlite:////var/data/defect_tracker.db` (persistent disk) |
| `UPLOADS_DIR` | Where uploaded defect photos are written/served from | On Render: `/var/data/uploads` (persistent disk) — see incident below |
| `APP_HOST` / `APP_PORT` | Local dev-only uvicorn bind settings | Unused on Render — the start command's `--host 0.0.0.0 --port $PORT` overrides this directly |
| `DISPLAY_TIMEZONE` | Timezone for displayed timestamps (storage is always UTC) | Default `America/New_York` |
| `MAX_UPLOAD_MB` | Photo upload size limit | Default 8 |
| `DEFECT_API_URL` | Where the MCP server reaches the REST API | Not used by the web app itself |
| `PRODUCTION_BRIEF_URL` | Base URL of the Eagle production brief JSON API | Real value: `http://20.62.194.32:8094` — a Render secret (`sync: false`) since it's internal infra |
| `SYNC_INTERVAL_MINUTES` | Interval `run_periodic_sync()` would use if ever re-enabled | Not currently driving anything automatic on Render (see §6) |
| `DEFAULT_COST_PER_DRAWER` | One-time seed value for the `app_settings` `cost_per_drawer` row | Has no effect after first startup — the Admin screen is authoritative afterward |
| `APP_USERNAME` / `APP_PASSWORD_HASH` | The single shared login credential | Secrets (`sync: false`); synced into `app_settings` on every startup (§5) |
| `RELAY_API_KEY` | Shared secret the relay scripts send via `X-Relay-Key` | Secret (`sync: false`); independent of the human login |
| `RENDER_URL` | The live app's own base URL | Only read by the local relay scripts, not by the FastAPI app itself |
| `PYTHON_VERSION` | Pins the Render Python runtime | `3.11.10` |

**`render.yaml`:** one `web` service (`env: python`, `plan: starter`), build
`pip install .`, start `alembic upgrade head && uvicorn app.main:app --host
0.0.0.0 --port $PORT`, one 1GB disk mounted at `/var/data`. Non-secret vars get
real default `value:`s in the file; secrets are declared `sync: false` so Render
prompts for them in its dashboard rather than storing them in git.

**Known gotchas discovered this session, and their resolution:**
- **`UPLOADS_DIR` persistent-disk incident:** adding `UPLOADS_DIR` to
  `render.yaml` with a plain default value did **not** make photos persistent.
  A plain `value:` in `render.yaml` is only read when a service is first created
  via Render's Blueprint flow — it is *not* retroactively applied to an
  already-existing service. The variable had to be entered directly in Render's
  dashboard before it actually took effect. This was diagnosed by uploading a
  fresh test photo live, fetching it back successfully, then re-fetching an
  *older* photo (still 404) — proving something had wiped the directory between
  the two uploads (a deploy), which only makes sense if the persistent-disk
  override wasn't actually active. Fixed by manually setting the var in Render's
  dashboard; verified end-to-end by uploading a photo, confirming it loads,
  triggering a real redeploy, and confirming that same photo still loaded
  afterward. `app/main.py` now logs a startup warning if `UPLOADS_DIR` or
  `DATABASE_URL` isn't actually set in the environment, so this exact failure
  mode is visible in logs from now on instead of silently discovered via a 404.
- **Login credential not updating after a Render dashboard change:** traced to
  the *original* credential-checking design reading `os.getenv(...)` directly
  with no stored copy — investigation found this was not a caching bug, but the
  most likely real-world cause (a stray newline/quote from pasting a hash into a
  dashboard text field, silently breaking `bcrypt.checkpw`) led to redesigning
  credential storage as described in §5 regardless, plus stripping quotes/whitespace.
- **Render cannot reach the production brief directly** — see §6; this is a
  network-level restriction on the production brief server's side, not something
  fixable from this app's code (see §8).

**Local machine scheduled tasks** (Windows Task Scheduler, supporting the relay):

| Task name | Schedule | What it does |
|---|---|---|
| `EagleDefectTracker-RelaySync` | Every 1 hour | Runs `scripts/relay_customer_issues.py` — unconditional fetch from the production brief + forward to `ingest-raw`. **Currently the only registered task.** |
| `EagleDefectTracker-RelayPoll` | *(not currently registered — removed this session)* | Would run `scripts/relay_poll.py` — the frequent heartbeat/on-demand-sync check-in described in §6. The script and server-side support still exist; only the schedule was removed. |

---

## 8. Known limitations / future improvement candidates

- **The root network restriction between Render and the production brief is
  still unresolved.** The local relay is a working workaround, not a fix — it
  depends on a machine on the office network staying on and the scheduled task
  running. If Blake/Scott's team opens the production brief server to Render's
  IP ranges (or provides another access path) in the future, the original
  direct-fetch code (`run_sync`/`run_periodic_sync`) is still in place and could
  be re-enabled in `app.main`'s lifespan.
- **"Sync Now" is currently slower than designed** (see §6) — the heartbeat
  poller that would make it near-instant isn't currently scheduled.
- **Uploaded photos are not covered by any backup process** — `scripts/backup_database.py`
  exists for the SQLite file but the `uploads/` persistent-disk directory isn't
  mentioned there; worth confirming whether it should be.
- **No per-user accounts** — by design (single shared login), but this means no
  way to know *which* staff member entered a given case beyond the cosmetic,
  unauthenticated role selector. Acceptable for the current single-shop pilot;
  flagged in `app/dependencies.py`'s own docstring as something to replace
  "before any LAN or multi-user deployment" beyond this pilot's scope.
- **`RENDER_URL`/relay credentials must be manually kept in sync** across the
  local `.env`, Render's dashboard, and (implicitly) anyone else's machine that
  might ever run the relay — there's no automated distribution of these values.
- **The Daily Summary auto-suggestion has no shift awareness** — `DefectCase`
  has no `shift` field, so `suggested_daily_counts()` computes at the whole-day
  level; a two-shift day would get identical suggestions on both shifts' forms
  (documented directly in that function's own docstring, not a surprise).

---

## 9. Testing & tooling

- **361 tests pass**, verified by actually running `pytest` just now — not
  estimated or recalled. Organized as:
  - `tests/unit/` — pure business-logic tests (counting, KPI math, status
    transitions, auth credential sync, sync payload processing, cost tracking,
    config, etc.) with no HTTP layer involved.
  - `tests/api/` — FastAPI `TestClient`-based end-to-end tests per router/feature
    area (auth, defect cases, daily production, reports, exports, customer
    issues, sync/ingest, health).
  - `tests/mcp/` — tests for the MCP server's tools.
- **Linting:** Ruff 0.8.4, configured in `pyproject.toml`
  (`select = ["E", "F", "I", "W", "B"]`, `ignore = ["B008"]`, line length 100,
  target Python 3.11). `ruff check .` and `ruff format --check .` both currently
  pass clean.
- Test isolation: an in-memory SQLite database per test (`StaticPool`), FastAPI
  dependency overrides for `get_db`, and a pre-authenticated session cookie
  attached to the shared `client` fixture so most tests can ignore auth entirely
  (the dedicated `tests/api/test_auth_api.py` uses its own unauthenticated
  fixture to test the login flow itself).

---

## 10. File/module structure

```
app/
  main.py                  FastAPI app instance, lifespan/startup, page routes, static/uploads mounts
  config.py                Settings (env vars), cached via @lru_cache
  database.py              SQLAlchemy engine/session setup, SQLite pragmas
  models.py                All ORM models (persistence only, no business rules)
  schemas.py                Pydantic request/response models
  seed_data.py             Baseline master data + credential sync, run on every startup
  auth_middleware.py       LoginRequiredMiddleware - gates every route except a small allowlist
  dependencies.py          Shared FastAPI dependencies (get_db re-export, get_actor_role)
  errors.py                Shared ServiceError hierarchy -> uniform JSON error envelope
  routers/
    auth.py                 Login / logout / logout-everywhere
    defect_cases.py          Create/list/edit/status/delete/photos for DefectCase
    daily_production.py     Daily Production Summary CRUD + suggested-counts
    reports.py               KPI summary, Pareto, trend, work-order history, rework queue
    customer_issues.py       Customer Issues CRUD + CSV export
    sync.py                  Production-brief sync control + relay ingest/heartbeat (see §6)
    master_data.py           Stations/categories/priorities/statuses/dispositions
    exports.py                Defect CSV export
    settings.py               Admin-editable app settings (cost_per_drawer)
  services/
    defect_service.py        Case numbering, creation, status transitions (see §3)
    metrics_service.py        KPI/Pareto/trend/sort math (see §4)
    customer_issue_service.py Customer Issue business rules
    sync_service.py           Production-brief sync + relay heartbeat/pending-request (see §6)
    auth_service.py           Login/session/credential-sync logic (see §5)
    settings_service.py       Generic app_settings read/write helpers
    audit_service.py          Writes AuditLog rows
    export_service.py         CSV export generation
  templates/                 Server-rendered Jinja2 pages (one per app/main.py page route)
  static/                    Plain CSS/JS, no build step
alembic/versions/            Six migrations: initial schema, customer-issue tables +
                              source_thread_id/sync_logs, resolved_on_the_spot/skipped_recheck,
                              app_settings/cost_per_drawer, auth_sessions
mcp_server/server.py         Stdio MCP server - calls the REST API, never touches SQLite directly
scripts/
  relay_customer_issues.py    Hourly full relay: fetch production brief -> forward to ingest-raw
  relay_poll.py                Frequent heartbeat/on-demand-sync poller (see §6 for current status)
  backup_database.py           SQLite backup utility
  seed_demo_data.py / seed_customer_issues.py  Synthetic data for local development
tests/                       unit/, api/, mcp/ - see §9
docs/                        PROJECT_SPEC.md + phase addenda, DATA_DICTIONARY.md, setup/user guides
render.yaml                  Render Blueprint (see §7)
.env.example                 Documented template for every environment variable (see §7)
pyproject.toml               Dependencies, Ruff config, pytest config, build config
```
