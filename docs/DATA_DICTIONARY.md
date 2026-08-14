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
| drawers_reworked | int >= 0 | soft rule (see PROJECT_SPEC.md 2.3) |
| drawers_scrapped | int >= 0 | soft rule (see PROJECT_SPEC.md 2.3) |
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
Drawer, Defects per 100 Drawers, Drawer Rejection Rate, First Pass Yield, Rework Rate,
Scrap Rate). All rates are `null`/"N/A" when `drawers_inspected` is 0.

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

Generic key-value settings store; not cost-specific by design.

### DailyProductionSummary additions
| Field | Type | Notes |
|---|---|---|
| cost_per_drawer_at_time | decimal(10,2), optional | snapshot of the configured rate at save time; `null` only for rows saved before Phase 4 and never re-saved since |

### KPI fields added to `/api/v1/reports/summary` and `/api/v1/reports/trend`
`internal_rework_cost`, `internal_scrap_cost`, `total_internal_quality_cost`,
`quality_cost_per_drawer_inspected` (`null` when `drawers_inspected` is 0 for the
period). See `PROJECT_SPEC_PHASE4.md` for the exact formulas and the
missing-snapshot fallback-rate rule.

### API (`/api/v1/settings`)
`GET /cost-per-drawer` · `PUT /cost-per-drawer` (`> 0`, audited).

### Configuration
`DEFAULT_COST_PER_DRAWER` (default `35.00`) — see `.env.example`. Seed-only; the
`app_settings` row is authoritative after first run.
