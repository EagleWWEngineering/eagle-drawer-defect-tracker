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
| disposition | enum, optional | Rework, Scrap, Use As Is, Hold |
| repair_action | text, optional | what actually happened (free text) |
| root_cause | text, optional | filled in later during investigation |
| corrective_action | text, optional | filled in later during investigation |
| notes | text, optional | |
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
Open, In Rework, Waiting, Ready for QC Recheck, Closed - Repaired, Closed - Scrapped,
Closed - Use As Is. "Ready for QC Recheck" is a legacy status kept for backward
compatibility only — the shop floor has no real recheck moment, so no UI presents
moving a case into it as an expected step (PROJECT_SPEC.md section 3.3). Allowed
transitions are enforced in `app/services/defect_service.py` (`STATUS_TRANSITIONS`,
`direct_close_statuses`) — see PROJECT_SPEC.md section 3.1.

## Counting definitions
See PROJECT_SPEC.md section 2.1 for the exact formulas (Defect Event, Defective
Drawer, Defects per 100 Drawers, Drawer Rejection Rate, First Pass Yield, Rework
Rate). All rates are `null`/"N/A" when `drawers_inspected` is 0. Scrap Rate was
dropped from the KPI/API/UI surface entirely in Phase 4's "Scrap removal" - see
that addendum.

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
`internal_rework_cost`, `total_internal_quality_cost`,
`quality_cost_per_drawer_inspected` (`null` when `drawers_inspected` is 0 for the
period). See `PROJECT_SPEC_PHASE4.md` for the exact formulas and the
missing-snapshot fallback-rate rule. `internal_scrap_cost`/`scrap_rate` were
dropped from this KPI surface entirely ("Scrap removal", below) — the
`DailyProductionSummary.drawers_scrapped` column and `Scrap` disposition/status are
both still kept for backward compatibility, just no longer surfaced anywhere.

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

## TEMPORARY: one-time real-data migration endpoint

`POST /api/v1/admin/import-data` (`app/routers/admin.py`,
`app/services/migration_service.py`, `scripts/export_real_data.py`) exists solely
to move real production data from the local dev SQLite database to the live
Render database, once, over HTTPS, using Rodolfo's real login. Protected like
every route by `LoginRequiredMiddleware`, plus requires re-entering the current
shared password in the request body (same pattern as
`POST /api/v1/auth/logout-everywhere`). Exports/imports `DefectCase`,
`DefectItem`, `DefectPhoto` (including file bytes, base64-encoded),
`StatusHistory`, `CustomerIssue`, and `DailyProductionSummary` only — never
`Station`/`DefectCategory`/`CustomerIssueCategory` (master data, seeded
identically by name on both sides) or `AuditLog`/`SyncLog` (environment-specific
operational logs). Every foreign key into a master-data table is carried across
by name and re-resolved to the target database's own id, since the two
databases' seeded ids are not guaranteed to match. Idempotent — safe to re-run.
This endpoint, the service module, and the export script are all slated for
**removal** in a follow-up commit once the real migration has been confirmed
successful on the live instance.
