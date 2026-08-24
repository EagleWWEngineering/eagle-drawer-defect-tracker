# Data Dictionary

Field-level reference for the Eagle Drawer Defect Tracker. Business rules and counting
definitions are in [`PROJECT_SPEC.md`](PROJECT_SPEC.md) — this document is the field
list and API contract, kept consistent with the SQLAlchemy models in `app/models.py`
and the Pydantic schemas in `app/schemas.py`.

## Tables

### Station (`stations`)
| Field | Type | Notes |
|---|---|---|
| id | int | primary key |
| name | string, unique | e.g. "Dado", "QC / Sorting / Shipping" |
| active | bool | deactivate instead of deleting if referenced |
| sort_order | int | display order |

### DefectCategory (`defect_categories`)
Same shape as Station: id, name (unique), active, sort_order.

### DailyProductionSummary (`daily_production_summaries`)
| Field | Type | Notes |
|---|---|---|
| id | int | primary key |
| production_date | date | |
| shift | string | default "Day" |
| drawers_inspected | int >= 0 | |
| drawers_rejected_unique | int >= 0 | hard rule: <= drawers_inspected |
| drawers_reworked | int >= 0 | soft rule (see PROJECT_SPEC.md 2.3); auto-suggested from DefectCase data, editable (see Phase 4 addendum) |
| drawers_scrapped | int >= 0 | soft rule (see PROJECT_SPEC.md 2.3); no longer a field on the Daily Summary form (Phase 4 "Scrap removal") - kept for backward compatibility, defaults to 0 for a new row or preserves the existing value when omitted from a PUT |
| notes | string, optional | required if a soft-rule warning is triggered |
| cost_per_drawer_at_time | decimal(10,2), optional | Phase 4 — rate snapshot at save time, see below |

Unique constraint: (production_date, shift).

### DefectCase (`defect_cases`)
| Field | Type | Notes |
|---|---|---|
| id | int | primary key |
| case_number | string, unique | `DF-YYYYMMDD-NNNN`, sequence resets per production_date |
| production_date | date | |
| detected_at | datetime (UTC) | |
| work_order_number | string | required, indexed |
| drawer_part_reference | string, optional | never required |
| found_station_id | FK -> stations.id | required — where the defect was found |
| possible_source_station_id | FK -> stations.id, optional | a HYPOTHESIS, never a confirmed root cause |
| priority | enum | Urgent, High, Normal |
| status | enum | see status list below |
| disposition | enum, optional | **Rework, Set Aside** as of Phase 7 (`PROJECT_SPEC_PHASE7.md`) — Use As Is/Hold/Scrap are retired for new writes, still valid on historical rows |
| repair_action | text, optional | what actually happened (free text) |
| root_cause | text, optional | filled in later during investigation |
| corrective_action | text, optional | filled in later during investigation |
| notes | text, optional | |
| skipped_recheck | bool | retired (Phase 7 — no recheck status exists); historical values kept, no longer written |
| cost_per_drawer_at_time | decimal(10,2), optional | Phase 7 — rate snapshot at creation time, see below; `null` only for cases created before Phase 7 |
| closed_at | datetime, optional | set when status becomes a Closed status |
| is_deleted | bool | soft delete only |

### DefectItem (`defect_items`)
| Field | Type | Notes |
|---|---|---|
| id | int | primary key |
| defect_case_id | FK | |
| defect_category_id | FK | |
| affected_drawer_quantity | int >= 1, default 1 | summed across items = Defect Events |
| notes | text, optional | |

Unique constraint: (defect_case_id, defect_category_id) — the service layer merges
duplicate categories into one item instead of creating a second row.

### DefectPhoto (`defect_photos`)
id, defect_case_id, stored_filename, original_filename, content_type, uploaded_at.
Allowed types: image/jpeg, image/png, image/webp. Max size: `MAX_UPLOAD_MB` (default 8 MB).
Files live on disk at `settings.uploads_dir / stored_filename` (`UPLOADS_DIR` env var,
default `./uploads` inside the project — see `app/config.py`). On Render this is set
to a subdirectory of the mounted persistent disk (`render.yaml`) rather than the
default, since the default path would sit on the ephemeral container filesystem and
be wiped on every redeploy.

### StatusHistory (`status_history`)
id, defect_case_id, from_status, to_status, note, changed_at.

### AuditLog (`audit_log`)
id, timestamp (UTC), actor_role, action, entity_type, entity_id, inputs_json,
before_json, after_json, success, message.

## Status list
**As of Phase 7** (`PROJECT_SPEC_PHASE7.md`), only three statuses are valid for a
new case or a new status change: `Open`, `Closed - Repaired`, `Closed - Use As Is`.
`In Rework`, `Waiting`, `Ready for QC Recheck`, and `Closed - Scrapped` are retired
for new entry — they remain valid **stored** values (a historical case in one of
them still renders/filters/exports correctly) but nothing writes them anymore.
Allowed transitions are enforced in `app/services/defect_service.py`
(`STATUS_TRANSITIONS`, `direct_close_statuses`, `VALID_STATUSES` vs.
`ALL_KNOWN_STATUSES`) — see `PROJECT_SPEC_PHASE7.md`.

## Counting definitions
See PROJECT_SPEC.md section 2.1 for Defect Event, Defective Drawer, Defects per
100 Drawers, Drawer Rejection Rate, and First Pass Yield - unchanged by Phase 7.
**Rework Rate** was redefined in Phase 7: `(cases with disposition "Rework" in the
filtered range) / drawers_inspected * 100` — no longer a
`DailyProductionSummary.drawers_reworked` sum. All rates are `null`/"N/A" when
`drawers_inspected` is 0. Scrap Rate was dropped from the KPI/API/UI surface
entirely in Phase 4's "Scrap removal" - see that addendum.

## REST API (`/api/v1`)

All responses are JSON. Errors use `{"error": {"message": "...", "field": "..."}}`
(field is `null` when the error isn't tied to one input). Interactive docs (OpenAPI):
`GET /docs` while the app is running.

| Method & path | Purpose |
|---|---|
| POST `/defect-cases` | Create a case with one or more defect items |
| GET `/defect-cases` | List/filter cases (date range, work order, category, station, priority, status, disposition) |
| GET `/defect-cases/{case_id}` | Get one case by internal id |
| GET `/defect-cases/by-number/{case_number}` | Get one case by its case number |
| PATCH `/defect-cases/{case_id}` | Edit a case / add items |
| POST `/defect-cases/{case_id}/status` | Change status (validated transition) |
| DELETE `/defect-cases/{case_id}` | Soft delete |
| POST `/defect-cases/{case_id}/photos` | Upload a photo |
| PUT `/daily-production/{production_date}` | Upsert the Daily Production Summary |
| GET `/daily-production` | List summaries |
| GET `/daily-production/{production_date}/suggested-counts` | Suggested Rejected/Reworked from real DefectCase data (Phase 4 "Scrap removal" / auto-calculation); read-only, never writes |
| GET `/rework-queue` | Priority-sorted open items |
| GET `/reports/summary` | KPI totals for a filtered date range |
| GET `/reports/pareto` | Pareto by category or possible source station |
| GET `/reports/trend` | Day/week trend |
| GET `/reports/work-orders/{work_order_number}` | Full history for one work order |
| GET `/master-data` | Stations, categories, priorities, statuses, dispositions |
| POST/PATCH `/master-data/stations`, `/master-data/defect-categories` | Admin edits |
| GET `/exports/defects.csv` | CSV export, respects the same filters as reports |
| GET `/health` | Liveness + DB check |

### Example: create a case with two categories

```json
POST /api/v1/defect-cases
{
  "production_date": "2026-07-24",
  "detected_at": "2026-07-24T14:30:00Z",
  "work_order_number": "WO-1024",
  "found_station_id": 15,
  "possible_source_station_id": 4,
  "priority": "High",
  "items": [
    {"defect_category_id": 8, "affected_drawer_quantity": 1},
    {"defect_category_id": 4, "affected_drawer_quantity": 1}
  ]
}
```

Response includes `"defect_event_count": 2` and `"case_number": "DF-20260724-0001"`.

## MCP tools

See [`MCP_SETUP.md`](MCP_SETUP.md) for connection instructions. Tool names and
behavior mirror the REST API exactly — read tools
(`get_defect_summary`, `get_defect_pareto`, `search_defect_cases`, `get_rework_queue`,
`get_work_order_defect_history`, `get_defect_case`) are read-only; write tools
(`record_defect_case`, `record_daily_production`, `update_defect_case_status`) call
the same REST endpoints as the browser UI and are fully audited.

## Phase 2: Customer Issues

Full addendum: [`PROJECT_SPEC_PHASE2.md`](PROJECT_SPEC_PHASE2.md). Summary:

### CustomerIssueCategory (`customer_issue_categories`)
Same shape as `DefectCategory`: id, name (unique), active, sort_order.

### CustomerIssue (`customer_issues`)
| Field | Type | Notes |
|---|---|---|
| id | int | primary key |
| issue_number | string, unique | `CI-YYYYMMDD-NNNN`, sequence resets per reported_date |
| reported_date | date | indexed |
| customer_name | string | required |
| order_number | string, optional | null = "order not identified" (a normal state) |
| issue_category_id | FK -> customer_issue_categories.id | |
| source_type | enum | `Manufacturing` or `Shipping Damage` |
| should_have_caught_at | string, optional | free text, e.g. "QA/Final" |
| piece_count | int >= 1, default 1 | |
| estimated_rework_cost | numeric(10,2), optional | auto = `piece_count * $100` if omitted |
| description | text | required |
| photo_urls | text, optional | comma-separated, for now |
| status | enum | `Open`, `Ignored`, `Linked` |
| linked_defect_case_id | FK -> defect_cases.id, optional | set when status = `Linked` |
| notes | text, optional | |
| is_deleted | bool | soft delete only |

### API (`/api/v1/customer-issues`)
`GET /categories` (order-sensitive: registered before `/{issue_id}`) ·
`POST ""` · `GET ""` (filters: date range, customer, order number, category, source
type, should_have_caught_at, status) · `GET /summary` (counts, cost totals, Escape
Rate, Internal Catch Rate) · `GET /pareto` (`group_by=category` or
`should_have_caught_at`) · `GET /{issue_id}` · `PATCH /{issue_id}` (status, notes,
order number resolution, `link_defect_case_id`) · `DELETE /{issue_id}` (soft delete) ·
`GET /api/v1/exports/customer-issues.csv`.

KPI formulas (Escape Rate, Internal Catch Rate) are in
`PROJECT_SPEC_PHASE2.md` — same zero-denominator-returns-null discipline as every
other rate in this app.

## Phase 3: Production Brief Sync

Full addendum: [`PROJECT_SPEC_PHASE3.md`](PROJECT_SPEC_PHASE3.md).

### CustomerIssue additions
| Field | Type | Notes |
|---|---|---|
| source_thread_id | string, unique, optional | production brief's `thread_id`; null = manually entered, never touched by sync |

### SyncLog (`sync_logs`)
id, sync_started_at, sync_completed_at, source_url, records_fetched,
records_created, records_updated, records_skipped, errors (text, optional), status
(`success` or `failed`). One row per sync attempt, whether it succeeds or fails.

### API (`/api/v1/sync`)
`POST /customer-issues` (trigger an immediate sync — the "Sync Now" button) ·
`GET /status` (most recent attempt, `null` if never run) ·
`GET /logs?limit=20` (recent attempts, for the Admin screen).

### Configuration
`PRODUCTION_BRIEF_URL` (default `http://20.62.194.32:8094`),
`SYNC_INTERVAL_MINUTES` (default `60`) — see `.env.example`.

## Phase 4: Internal Cost Tracking

Full addendum: [`PROJECT_SPEC_PHASE4.md`](PROJECT_SPEC_PHASE4.md).

### AppSetting (`app_settings`)
| Field | Type | Notes |
|---|---|---|
| key | string | primary key, e.g. `"cost_per_drawer"` |
| value | string | |
| updated_at | datetime (UTC) | |

Generic key-value settings store; not cost-specific by design. Phase 5 reuses it
for the shared-login credential — see `auth_username` / `auth_password_hash` below.

### DailyProductionSummary additions
| Field | Type | Notes |
|---|---|---|
| cost_per_drawer_at_time | decimal(10,2), optional | snapshot of the configured rate at save time; `null` only for rows saved before Phase 4 and never re-saved since |

### KPI fields added to `/api/v1/reports/summary` and `/api/v1/reports/trend`
**Superseded by Phase 7** (`PROJECT_SPEC_PHASE7.md`, "Cost model" section below) -
`internal_rework_cost` is now derived from `DefectCase.cost_per_drawer_at_time`,
not from this table's `drawers_reworked` * rate. Kept here for history:
`internal_rework_cost`, `total_internal_quality_cost`,
`quality_cost_per_drawer_inspected` (`null` when `drawers_inspected` is 0 for the
period). `internal_scrap_cost`/`scrap_rate` were dropped from this KPI surface
entirely ("Scrap removal", below) — the `DailyProductionSummary.drawers_scrapped`
column and `Scrap` disposition/status are both still kept for backward
compatibility, just no longer surfaced anywhere.

### API (`/api/v1/settings`)
`GET /cost-per-drawer` · `PUT /cost-per-drawer` (`> 0`, audited).

### Configuration
`DEFAULT_COST_PER_DRAWER` (default `35.00`) — see `.env.example`. Seed-only; the
`app_settings` row is authoritative after first run.

### Scrap removal (see `PROJECT_SPEC_PHASE4.md` for the full rationale)
Defective drawers on this floor are reworked or reused; scrap essentially doesn't
happen and staff can't reliably track it. Scrap Rate, Internal Scrap Cost, and any
scrap column/chart element were removed from the Dashboard, Reports, and the Daily
Summary form. `Total Internal Quality Cost` = `Internal Rework Cost` only now. The
`drawers_scrapped` column, the CSV `day_internal_scrap_cost` column, and the `Scrap`
disposition/status are all kept in the data model/API for backward compatibility
(no destructive migration) - `drawers_scrapped` on `DailyProductionSummaryIn` is
optional; omitting it preserves whatever was already saved instead of zeroing it.

Also new in this release: `drawers_rejected_unique`/`drawers_reworked` on the Daily
Summary form are pre-filled from real `DefectCase` data (see the
`/daily-production/{date}/suggested-counts` endpoint and
`app/services/defect_service.py suggested_daily_counts()`), still fully editable,
and never silently overwritten once a row is saved for that date/shift.

## Phase 5: Authentication

Single shared login for the whole app (no per-user accounts, no roles) - see
`app/services/auth_service.py`, `app/auth_middleware.py`, `app/routers/auth.py`.

### AuthSession (`auth_sessions`)
| Field | Type | Notes |
|---|---|---|
| id | int | primary key |
| token | string, unique, indexed | opaque, `secrets.token_urlsafe(32)` |
| created_at | datetime (UTC) | informational only - never used to expire a session |

No `expires_at`/TTL column: a session is valid for as long as its row exists, with
no time-based expiry check anywhere. It's deleted only by an explicit "Log out"
(this row only) or "Log out everywhere" (every row).

### Configuration
`APP_USERNAME`, `APP_PASSWORD_HASH` (a bcrypt hash, never the plaintext password) —
see `.env.example`. These are the environment-variable *source*; the value actually
checked at login lives in `app_settings` under the keys `auth_username` /
`auth_password_hash` (both `string`), kept in sync with the environment on every
app startup by `sync_credentials_from_env()` — not just once against an empty
database. See "Credentials" in `PROJECT_SPEC_PHASE5.md` for why this is DB-backed
rather than a direct `os.getenv` read.

### API (`/api/v1/auth`) — all unauthenticated except `/login`
`POST /login` (`{"username", "password"}` → sets the `eagle_session` cookie,
httponly, not Secure since the app runs over plain HTTP on the LAN, ~10-year
max-age) · `POST /logout` (ends only this device's session) ·
`POST /logout-everywhere` (`{"password"}`, re-confirms the current password, then
deletes every session row at once).

Every other route (UI pages and API endpoints) requires a valid session cookie,
enforced once by `LoginRequiredMiddleware`, except `GET /api/v1/health` and static
assets under `/static/`.

A one-time real-data migration endpoint (`POST /api/v1/admin/import-data`) existed
briefly to move real production data from the local dev SQLite database to the
live Render database; it has been removed now that the migration is complete (see
git history around the commit removing `app/routers/admin.py` if this ever needs
to be understood again).

## Phase 6: Scheduled vs Completed Drawers

Full addendum: `PROJECT_SPEC_PHASE6.md`. Source discovery writeup (why this is an
HTML scrape, not a JSON API call): `PRODUCTION_BRIEF_SCHEDULE_SOURCE.md`.

### DailySchedule (`daily_schedules`)
| Field | Type | Notes |
|---|---|---|
| production_date | date, primary key | one row per calendar date - a whole-day figure, unlike `daily_production_summaries` (per-shift) |
| drawers_scheduled | int >= 0 | |
| source | string, `"sync"` \| `"manual"` | manual always wins - a sync write is skipped entirely against a `"manual"` row |
| synced_at | datetime (UTC), nullable | last successful relay write; null if never synced |
| updated_at | datetime (UTC) | |

### API (`/api/v1/daily-production`, `/api/v1/sync`, `/api/v1/reports`)
- `GET /daily-production/schedule` (`?date=` or `?start_date=&end_date=`) - a date
  with no row is simply absent from the response, never a `0`.
- `PUT /daily-production/schedule` - manual entry/override, always `source="manual"`.
- `GET /daily-production/schedule-attainment` (`?start_date=&end_date=`) -
  `{days: [{production_date, drawers_scheduled, drawers_inspected}], total_scheduled,
  total_inspected, attainment_pct}`. `attainment_pct` is `null` when `total_scheduled`
  is `0` or unknown (no known day in range).
- `POST /sync/daily-schedule/ingest-raw` (`X-Relay-Key`, reuses `RELAY_API_KEY`) -
  relay ingest, mirrors `/sync/customer-issues/ingest-raw`'s auth/shape exactly.
- `GET /reports/date-preset` (`?preset=today|yesterday|last_7_days|last_30_days|month_to_date`)
  - resolves a Dashboard preset button to `{start_date, end_date}` in
  `DISPLAY_TIMEZONE`, server-side (`app/timezone_utils.py resolve_date_preset`).

### `TrendPointOut` additions (`/api/v1/reports/trend`)
`drawers_scheduled` (int, nullable) and `schedule_attainment_pct` (float, nullable),
bucketed the same way (`day`/`week`) as every other field on this response.

### Defect CSV export additions (`/api/v1/exports/defects.csv`)
`day_drawers_scheduled`, `day_schedule_attainment_pct` - joined by `production_date`;
blank (not `0`) for a date with no `daily_schedules` row. (The Phase 4 cost columns
this mirrored, `day_cost_per_drawer` / `day_internal_rework_cost`, were themselves
replaced by per-case columns in Phase 7 - see below - but these schedule columns are
unaffected and unchanged.)

### Configuration
No new environment variable - the relay's schedule-scrape pass reuses
`PRODUCTION_BRIEF_URL` and `RELAY_API_KEY`, both already present for the Phase 3
customer-issues relay.

## Phase 7: Cost Model + Disposition/Status Simplification

Full addendum: `PROJECT_SPEC_PHASE7.md`. Vocabulary and display change plus one
narrow, logged migration of currently-open cases - no table/column drops, no
destructive backfills.

### `DefectCase` additions / changes
| Field | Type | Notes |
|---|---|---|
| cost_per_drawer_at_time | decimal(10,2), optional | rate snapshot at creation; `null` only for cases created before this column existed, in which case cost calculations fall back to the currently-configured rate |

`disposition` is now `Rework` \| `Set Aside` for new writes (`Use As Is`/`Hold`/
`Scrap` retired); `status` is now `Open` \| `Closed - Repaired` \| `Closed - Use As
Is` for new writes (`In Rework`/`Waiting`/`Ready for QC Recheck`/`Closed -
Scrapped` retired). `skipped_recheck` is retired (no recheck status exists) - the
column stays, historical values stay, nothing writes to it anymore.

### Migration (the only data change)
`alembic` revisions `3d8532f3a9ec` (schema) + `7c1f9a2b4e6d` (data). Only
currently-open (non-closed) cases in a retired status move to `Open`, with a real
`status_history` row and an `audit_log` row; an open case with a retired
disposition gets it remapped to `Set Aside`, logged the same way. Closed cases -
including ones already carrying a retired disposition - are never touched. See
`tests/unit/test_phase7_migration.py`.

### Cost model
Replaces the Phase 4 dual-source model entirely: one cost unit per `DefectCase`
(its own `cost_per_drawer_at_time` snapshot, or the current rate as a fallback),
zero for a case closed `Closed - Use As Is`. Never multiplied by `DefectItem`
count or `affected_drawer_quantity`.

### KPI fields changed on `/api/v1/reports/summary` and `/api/v1/reports/trend`
- `drawers_reworked` - repurposed: count of cases with disposition `"Rework"` in
  the filtered range (Rework Rate's numerator), not a `DailyProductionSummary` sum.
- `internal_rework_cost` - now case-derived (see "Cost model" above).
- `cost_avoided` - **new**: summed cost of every case in range that closed
  `Closed - Use As Is`.
- Removed: `defect_case_rework_count`, `cost_basis` (the dual-source model they
  described no longer exists), `queued_rework_count`, `skipped_recheck_count`,
  `pct_queued_rework_closed_without_recheck` (no recheck status exists).

### Defect CSV export changes (`/api/v1/exports/defects.csv`)
`day_cost_per_drawer` / `day_internal_rework_cost` (date-joined from
`DailyProductionSummary`) are **replaced** by per-case columns: `case_cost_per_drawer`,
`case_internal_cost`, `case_cost_avoided` - computed from each row's own case, not a
date join, since cost no longer depends on whether a Daily Production Summary
exists for that date.

### `DailyProductionSummaryIn`/`Out` changes
`drawers_reworked` is now optional (`int | None`, default `None`) on the input -
same "omit to preserve, explicit int to override" rule `drawers_scrapped` already
had since Phase 4. `DailyProductionSummaryOut`'s `internal_rework_cost` /
`internal_scrap_cost` computed fields were removed (they multiplied
`drawers_reworked`/`drawers_scrapped` by the row's rate snapshot - exactly the
retired per-date model); `cost_per_drawer_at_time` itself stays as a plain,
informational field.

### `MasterDataOut` additions
`all_statuses`, `all_dispositions` - every historically-possible value, retired
ones included, for filter/display dropdowns only (Reports/Dashboard). `statuses`/
`dispositions` stay write-legal-only (what a case can be created/changed to).

### `DailyProductionSummaryOut` addition: `reworked_case_count`
Read-only, not stored on the row - a per-`production_date` count of `DefectCase`
rows with disposition `"Rework"` (`app/services/defect_service.py
count_rework_cases_by_date`), the same rule Rework Rate itself uses (no status
qualifier). Added to the Daily Summary page's Recent Entries table as "Reworked
(from cases)" after Rodolfo asked for a reference figure once the editable
`drawers_reworked` input left the form - not part of the save payload, purely
informational.
