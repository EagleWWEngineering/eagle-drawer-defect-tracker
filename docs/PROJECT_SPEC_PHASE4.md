# Phase 4 Addendum — Internal Defect Cost Tracking

Addendum to [`PROJECT_SPEC.md`](PROJECT_SPEC.md),
[`PROJECT_SPEC_PHASE2.md`](PROJECT_SPEC_PHASE2.md), and
[`PROJECT_SPEC_PHASE3.md`](PROJECT_SPEC_PHASE3.md). Everything in all three still
applies unchanged. This document covers only what Phase 4 added: a configurable
flat cost-per-drawer so the app can answer "how much did rework and scrap cost us
this week?" alongside the existing rate-based KPIs.

## Purpose and scope

- A single admin-editable setting, `cost_per_drawer` (default `$35.00`, seeded from
  `DEFAULT_COST_PER_DRAWER` in `.env`), multiplies against `drawers_reworked` and
  `drawers_scrapped` on each `DailyProductionSummary` row to produce an internal
  rework/scrap cost figure.
- This is entirely additive: no existing Phase 1 defect case logic, status
  transitions, or counting rules changed; no existing Phase 2 customer issue or
  Phase 3 sync functionality changed. The Phase 2 **Estimated External Rework
  Cost** (`piece_count * $100`) is a completely separate number on a separate
  table and is never read or written by Phase 4 code — the two costs are only
  ever added together client-side, for display, never stored together.

## Data model summary

- New `AppSetting` table: `key` (primary key), `value` (string), `updated_at`. A
  generic key-value store, deliberately not cost-specific, so future settings
  don't need their own tables. Seeded once with `key="cost_per_drawer"` from
  `get_settings().default_cost_per_drawer`; the DB row is authoritative after
  first run — editing `.env` afterward has no effect until the row is deleted.
- `DailyProductionSummary` gains `cost_per_drawer_at_time` (`Numeric(10,2)`,
  nullable). This is a **snapshot**, stamped with whatever rate is currently
  configured every time a row is created or updated via
  `defect_service.upsert_daily_summary`. It is nullable only because rows saved
  before Phase 4 shipped have no snapshot; any row saved (or re-saved) after
  Phase 4 always gets one. Changing the rate in Admin never rewrites
  `cost_per_drawer_at_time` on existing rows — that is the entire point of
  storing it per-row instead of computing everything from the current setting.

## KPI formulas (exact, used identically everywhere)

- **Internal Rework Cost** = sum over the period of
  `drawers_reworked * cost_per_drawer_at_time` for each `DailyProductionSummary`
  row (falling back to the *currently configured* rate for any row that predates
  Phase 4 and has no snapshot — see "Fallback rate" below).
- **Internal Scrap Cost** = same formula, using `drawers_scrapped`.
- **Total Internal Quality Cost** = Internal Rework Cost + Internal Scrap Cost.
- **Quality Cost per Drawer Inspected** = Total Internal Quality Cost /
  `drawers_inspected` for the period. `null`/"N/A" when `drawers_inspected` is 0
  for the period, same null-vs-zero discipline as every other per-drawer rate in
  `PROJECT_SPEC.md` section 2.1.
- **Total Quality Cost (Internal + External)** = Total Internal Quality Cost +
  Phase 2's Estimated External Rework Cost. Computed client-side (dashboard JS
  sums the two already-fetched API responses) — there is no server-side field
  for this combined figure, to avoid coupling `metrics_service.py` (internal) to
  `customer_issue_service.py` (external).

### Fallback rate

Historical rows saved before Phase 4 have `cost_per_drawer_at_time = null`. Any
KPI or CSV export that sums cost across a date range uses the **currently
configured** rate for those rows rather than treating their cost as $0 — a
pre-Phase-4 day with real rework/scrap still cost the shop money, and silently
zeroing it would understate cost more than an approximation would. This is
implemented once, in `metrics_service.sum_internal_quality_costs(entries, *,
fallback_rate)`, and reused by every caller (reports summary, reports trend,
CSV export) so the rule can't drift between call sites.

## Configuration

`DEFAULT_COST_PER_DRAWER` in `.env` (default `35.00`) is the seed value used only
the first time the app starts against a fresh database. After that, the rate
lives in the `app_settings` table and is only ever changed through Admin (or
directly via the API below).

## API

- `GET /api/v1/settings/cost-per-drawer` → `{"cost_per_drawer": 35.00}`.
- `PUT /api/v1/settings/cost-per-drawer` (body `{"cost_per_drawer": 40.00}`,
  must be `> 0`) → same shape. Recorded in the audit log
  (`entity_type="AppSetting"`, `entity_id="cost_per_drawer"`).
- `GET /api/v1/reports/summary` and `GET /api/v1/reports/trend` (existing
  routes, unchanged paths) now include the four cost fields described above.
- `POST`/`GET` on `/api/v1/daily-production` (existing route) now returns
  `cost_per_drawer_at_time`, `internal_rework_cost`, `internal_scrap_cost` on
  each row — the latter two are `null` (not `0`) for pre-Phase-4 rows that have
  never been re-saved.
- `GET /api/v1/exports/defect-items.csv` (existing route) gains three columns:
  `day_cost_per_drawer`, `day_internal_rework_cost`, `day_internal_scrap_cost`,
  attributed by the defect item's `production_date`. When a date has more than
  one shift's `DailyProductionSummary` row, the cost columns are the **sum**
  across all of that date's shifts (not one arbitrarily chosen shift), with the
  displayed rate being the first non-null snapshot found for that date (or the
  fallback rate if none of that date's rows have one).

## UI

- **Dashboard**: a "Quality Cost (today)" card showing Internal Rework Cost,
  Internal Scrap Cost, Total Internal Quality Cost, and Total Quality Cost
  (Internal + External).
- **Daily Production Summary**: the current rate is shown as a read-only
  reference line above the form (`Current rate: $X.XX/drawer`) so staff know
  what will be applied *before* they save; after a successful save, a
  confirmation card shows the calculated rework/scrap cost and the rate that was
  actually used for that entry. The "Recent entries" table has Rework Cost and
  Scrap Cost columns.
- **Reports**: the Summary section gains Rework Cost, Scrap Cost, and Total
  Internal Quality Cost tiles; a new "Internal quality cost trend" chart plots
  rework/scrap cost per period alongside the existing defect-count trend chart;
  the CSV export includes the three cost columns described above.
- **Admin**: a "Cost Settings" section with an editable "Current cost per
  drawer" field and a note that changing it only affects future calculations —
  historical Daily Production Summaries keep the rate that was active when they
  were saved.

## MCP

`get_defect_summary` (`mcp_server/server.py`) needed no code change beyond its
docstring: it forwards the full `/api/v1/reports/summary` JSON body, so the four
cost fields are already present in its response the same way every other KPI
field is. `tests/mcp/test_mcp_tools.py` asserts the tool's result still equals a
direct call to the same endpoint, which covers this by construction.
