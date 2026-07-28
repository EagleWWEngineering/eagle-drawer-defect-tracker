# CLAUDE.md — Eagle Drawer Defect Tracker

Full specification: `docs/PROJECT_SPEC.md` (business rules, data model, counting
definitions, phases). Phase 2 addendum (Customer Issues tab):
`docs/PROJECT_SPEC_PHASE2.md`. Phase 3 addendum (production brief sync):
`docs/PROJECT_SPEC_PHASE3.md`. Phase 4 addendum (internal defect cost tracking):
`docs/PROJECT_SPEC_PHASE4.md`. Field-level detail: `docs/DATA_DICTIONARY.md`. This
file is the short version for coding agents working in this repo.

Internal quality cost (`AppSetting`/`cost_per_drawer`,
`app/services/settings_service.py`, `/api/v1/settings/cost-per-drawer`) is a flat
admin-editable rate multiplied against `drawers_reworked`/`drawers_scrapped` on
`DailyProductionSummary`. See `docs/PROJECT_SPEC_PHASE4.md` before changing it.
It is a completely separate number from Phase 2's `estimated_rework_cost`
(`piece_count * $100`) — never conflate the two rates or their tables.

Customer Issues (`CustomerIssue`/`CustomerIssueCategory`,
`app/services/customer_issue_service.py`, `/api/v1/customer-issues`) is a separate
data type from internal defect cases — see `docs/PROJECT_SPEC_PHASE2.md` before
changing it, and never merge its rules into `defect_service.py`.

Customer Issues are synced hourly from the production brief
(`app/services/sync_service.py`, `/api/v1/sync/*`) — see `docs/PROJECT_SPEC_PHASE3.md`
before changing dedup/mapping logic. `source_thread_id` null = manual entry, never
touched by sync. A synced issue's `linked_defect_case_id` and any status past `Open`
are local staff decisions and must never be overwritten by a later sync.

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
- No "Remake" disposition — only Rework, Scrap, Use As Is, Hold. `repair_action` is
  free text describing what actually happened.
- Status transitions are enforced by the map in `app/services/defect_service.py` (see
  `docs/PROJECT_SPEC.md` §3.1) — do not bypass it in routers or the UI.
- Soft delete only for `DefectCase`. Master data (`Station`, `DefectCategory`) can be
  deactivated but never hard-deleted if referenced by historical records.
- Counting formulas (defects/100, rejection rate, FPY, rework rate, scrap rate) must
  match `docs/PROJECT_SPEC.md` §2.1 exactly, everywhere. Zero `drawers_inspected` →
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
