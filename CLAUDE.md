# CLAUDE.md — Eagle Drawer Defect Tracker

Full specification: `docs/PROJECT_SPEC.md` (business rules, data model, counting
definitions, phases). Phase 2 addendum (Customer Issues tab):
`docs/PROJECT_SPEC_PHASE2.md`. Phase 3 addendum (production brief sync):
`docs/PROJECT_SPEC_PHASE3.md`. Phase 4 addendum (internal defect cost tracking,
plus the later "Scrap removal" section): `docs/PROJECT_SPEC_PHASE4.md`. Phase 5
addendum (single shared login): `docs/PROJECT_SPEC_PHASE5.md`. Phase 6 addendum
(scheduled vs completed drawers, synced from the production brief):
`docs/PROJECT_SPEC_PHASE6.md`. Phase 7 addendum (cost model + disposition/status
simplification): `docs/PROJECT_SPEC_PHASE7.md`. Field-level detail:
`docs/DATA_DICTIONARY.md`. This file is the short version for coding agents
working in this repo.

Internal quality cost (`AppSetting`/`cost_per_drawer`,
`app/services/settings_service.py`, `/api/v1/settings/cost-per-drawer`) is a flat
admin-editable rate. As of Phase 7, cost is snapshotted per `DefectCase` at
creation (`DefectCase.cost_per_drawer_at_time`) — one unit per case, zero for a
case closed `Closed - Use As Is` — not multiplied against
`DailyProductionSummary.drawers_reworked` anymore; see
`docs/PROJECT_SPEC_PHASE7.md` "Cost model" before changing it.
`DailyProductionSummary.cost_per_drawer_at_time`/`drawers_reworked`/
`drawers_scrapped` all stay in the schema with their historical values, but
nothing reads them for cost anymore. It is a completely separate number from
Phase 2's `estimated_rework_cost` (`piece_count * $100`) — never conflate the
two rates or their tables.

Every route (UI pages and API endpoints) requires the single shared login except
`GET /api/v1/health` and static assets — see `app/auth_middleware.py`,
`app/services/auth_service.py`, and `docs/PROJECT_SPEC_PHASE5.md`. There are no
per-user accounts or roles in this login; the "Role (prototype)" selector in the
header is a separate, unrelated, cosmetic label — never conflate the two.

Customer Issues (`CustomerIssue`/`CustomerIssueCategory`,
`app/services/customer_issue_service.py`, `/api/v1/customer-issues`) is a separate
data type from internal defect cases — see `docs/PROJECT_SPEC_PHASE2.md` before
changing it, and never merge its rules into `defect_service.py`.

Customer Issues are synced hourly from the production brief
(`app/services/sync_service.py`, `/api/v1/sync/*`) — see `docs/PROJECT_SPEC_PHASE3.md`
before changing dedup/mapping logic. `source_thread_id` null = manual entry, never
touched by sync. A synced issue's `linked_defect_case_id` and any status past `Open`
are local staff decisions and must never be overwritten by a later sync.

Daily schedule (`DailySchedule`, `app/services/schedule_service.py`,
`/api/v1/daily-production/schedule*`) is also synced from the production brief, via
the same relay script — see `docs/PROJECT_SPEC_PHASE6.md` and
`docs/PRODUCTION_BRIEF_SCHEDULE_SOURCE.md` before changing it. `source == "manual"`
means a human edited it on the Daily Summary form; the relay must never overwrite
that row (manual-wins rule in `schedule_service.upsert_schedule`).

"Working day" (Eagle runs Mon–Fri) has exactly one definition, in
`app/services/working_days_service.py` (`is_working_day`/`working_day_set`/
`walk_back_working_days`) — never reimplement it (not even a bare
`weekday() < 5` check) anywhere else. A date counts if it has a scheduled or
inspected figure > 0, or it's a plain Mon–Fri weekday that wasn't explicitly
recorded as a scheduled-zero-and-nothing-inspected holiday; a missing schedule
row (failed scrape) is never treated as a holiday. `schedule_service`'s sync
ingest path rejects a non-working-day date before writing (manual writes are
unaffected — the deliberate overtime-Saturday escape hatch);
`metrics_service.build_schedule_vs_completed` and the Reports trend omit
weekends and flag (never silently drop) a working weekday holiday; the
Yesterday/Last 7/Last 30 days date presets walk back working days, not
calendar days (`working_days_service.resolve_working_day_preset` —
`timezone_utils.resolve_date_preset` stays pure/DB-free and only handles
Today/Month to date).

Dispositions (`Rework`, `Set Aside`) and statuses (`Open`, `Closed - Repaired`,
`Closed - Use As Is`) are a small, fixed vocabulary as of Phase 7 — see
`docs/PROJECT_SPEC_PHASE7.md` before changing either list.
`app/services/defect_service.py`'s `VALID_STATUSES`/`VALID_DISPOSITIONS` are what
new writes must use; `ALL_KNOWN_STATUSES`/`ALL_KNOWN_DISPOSITIONS` (which also
include the retired `In Rework`/`Waiting`/`Ready for QC Recheck`/`Closed -
Scrapped`/`Use As Is`/`Hold`) are for filter/display surfaces only, never for
write validation — a retired value stays valid stored data forever, never a new
write.

## What this is

A local FastAPI + SQLite app for tracking drawer defects at Eagle Woodworking's
drawer-production pilot, plus a stdio MCP server that calls the same REST API. No
frontend framework — Jinja2 + plain HTML/CSS/JS. No cloud dependency; must run fully
offline after install.

## Non-negotiable business rules

- Work Order Number is the only required production identifier. No separate required
  Job Number / Drawer ID.
- One category on one drawer = one defect event, however many physical defects of
  that category. Two categories on one drawer = two defect events, one defective
  drawer. `affected_drawer_quantity` on `DefectItem` counts multiple drawers with the
  same category.
- Reinspection of an unresolved defect updates the existing `DefectCase` +
  `StatusHistory`; it never creates a duplicate case.
- `found_station_id` (fact) and `possible_source_station_id` (hypothesis) are separate
  fields. Never call possible source station a root cause anywhere — UI, API, MCP,
  docs.
- Priority/status always render with a text label, never color alone.
- No operator-name field anywhere. This is a process tool, not a performance tool.
- No "Remake" disposition — only Rework and Set Aside as of Phase 7 (Scrap/Use As
  Is/Hold are retired for new entry, still valid on historical rows). `repair_action`
  is free text describing what actually happened.
- Status transitions are enforced by the map in `app/services/defect_service.py` (see
  `docs/PROJECT_SPEC_PHASE7.md`) — do not bypass it in routers or the UI.
- Soft delete only for `DefectCase`. Master data (`Station`, `DefectCategory`) can be
  deactivated but never hard-deleted if referenced by historical records.
- Counting formulas (defects/100, rejection rate, FPY) must match
  `docs/PROJECT_SPEC.md` §2.1 exactly, everywhere. Rework Rate and Internal Quality
  Cost were redefined in Phase 7 (`docs/PROJECT_SPEC_PHASE7.md`) — case-derived, not
  a Daily Production Summary field. There is no scrap rate/cost anywhere in this app
  (`docs/PROJECT_SPEC_PHASE4.md` "Scrap removal"). Zero `drawers_inspected` →
  `null`/"N/A", never a divide-by-zero.

## Architecture rule

UI → API routers (HTTP I/O only) → service layer (all business logic) → DB layer.
The MCP server calls the REST API — it must never write to SQLite directly. This is
how the UI and MCP share one source of truth for business logic.

## Build & run

```bash
pip install -e ".[dev]"
alembic upgrade head
python scripts/seed_demo_data.py   # optional synthetic demo data
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
Windows: `run_app.bat`. macOS/Linux: `./run_app.sh`.

MCP server (separate process): `python -m mcp_server.server` (reads `DEFECT_API_URL`,
default `http://127.0.0.1:8000`). Setup for Codex/Claude Code: `docs/MCP_SETUP.md`.

## Test & lint

```bash
pytest
ruff check .
ruff format --check .
```

## Done means

All tests pass, Ruff passes, `alembic upgrade head` applies cleanly to a fresh DB, the
app starts with the one documented command, the core workflow works end to end at
desktop and mobile widths, and no required functionality has a TODO placeholder.
