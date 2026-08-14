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

### Defect-case fallback (dual-source cost)

`DailyProductionSummary` counts are the *official* source when they exist — a
manually-entered daily count may include rework/scrap that never got a defect
case. But if QC logs a `DefectCase` with a `Rework`/`Scrap` disposition (or a
`Closed - Repaired`/`Closed - Scrapped` status) for a production date that has
**no** `DailyProductionSummary` row at all, cost must not silently show $0 just
because nobody filled out the daily form for that date.

`metrics_service.compute_internal_quality_cost(*, daily_summary_entries,
fallback_case_counts, fallback_rate, has_daily_summary_rows)` resolves this per
production date:

- Dates that **have** a `DailyProductionSummary` row use it, exactly as before
  (Fallback rate section above still applies to old rows within it).
- Dates that have **no** `DailyProductionSummary` row at all fall back to
  counting defect cases: one case with disposition `Rework` (or status
  `Closed - Repaired`) counts as one reworked drawer; one case with disposition
  `Scrap` (or status `Closed - Scrapped`) counts as one scrapped drawer — see
  `metrics_service.classify_case_cost_bucket` (scrap wins if a case shows both
  signals, since the final physical outcome is what actually cost money).
  Case-derived cost always uses the *currently configured* rate, since a
  `DefectCase` has no per-row rate snapshot of its own.
- The two sources are never combined for the same date — a date with a summary
  row is never also counted through the case fallback, so nothing is double
  counted.

`KpiOut` (from `/reports/summary`) and each `/reports/trend` point expose the
resulting `defect_case_rework_count`, `defect_case_scrap_count`, and
`cost_basis` (`"daily_summary"` | `"defect_cases"` | `"blended"` | `"none"`), so
the Dashboard and Reports Summary cards can show "Based on daily summary",
"Based on N defect cases (no daily summary recorded)", or a blended note,
instead of leaving the user to guess where a number came from.

The CSV export (`/api/v1/exports/defect-items.csv`) intentionally keeps its
original per-row, per-date behavior described below and does **not** apply this
fallback — its cost columns are specifically "the daily-summary rate/cost that
applied on this defect's date", not a period total.

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

- **Dashboard**: a "Quality Cost" card showing Internal Rework Cost, Internal
  Scrap Cost, Total Internal Quality Cost, and Total Quality Cost (Internal +
  External), scoped to whatever date range is selected in the dashboard's date
  range filter (Start/End date fields, quick-select buttons for Today/This
  Week/This Month/Last 30 Days, default last 7 days). A note under the card
  (`costBasisLabel` in `app/static/js/app.js`) states which source drove the
  number — daily summary, N defect cases, or a blend of both.
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
docstring: it forwards the full `/api/v1/reports/summary` JSON body, so the cost
fields are already present in its response the same way every other KPI field is.
`tests/mcp/test_mcp_tools.py` asserts the tool's result still equals a direct call
to the same endpoint, which covers this by construction.

## Scrap removal

Later revision: defective drawers on this floor are reworked or reused — scrap
essentially doesn't happen and staff can't reliably track a scrap count. What
matters is the existing Internal Catch Rate metric (Phase 2), not a scrap number.

- **Daily Production Summary form**: no `drawers_scrapped` field anymore. Fields
  are Drawers Inspected (manual, required), Unique Drawers Rejected, Drawers
  Reworked. The latter two are pre-filled from real `DefectCase` data for the
  given production date (see "Auto-calculation" below), still fully editable, and
  never silently overwritten by re-opening an already-saved entry.
- **Dashboard**: the Scrap Rate KPI tile and the Internal Scrap Cost card are gone.
- **Reports**: Scrap Rate is gone from the summary tiles; the Scrap Cost column is
  gone from the records table and the CSV export; the cost trend chart plots
  rework cost only.
- **Cost calculation**: `Total Internal Quality Cost` = `Internal Rework Cost`
  only (the scrap term was dropped from the sum). `Total Quality Cost (Internal +
  External)` is computed from that updated total. `cost_per_drawer` itself is
  unaffected — still used for rework cost. `internal_scrap_cost` and `scrap_rate`
  no longer appear anywhere in `KpiOut`/`TrendPointOut`; `metrics_service.py`'s
  `sum_internal_rework_cost` / `compute_internal_quality_cost` /
  `defect_case_derived_rework_count` replaced their two-value (`rework, scrap`)
  predecessors.
- **Data model**: NOT a destructive migration. `DailyProductionSummary.drawers_scrapped`
  and the `Scrap` disposition/status both still exist for backward compatibility -
  only the UI/API surface stopped asking for, calculating, and displaying them.
  `DailyProductionSummaryIn.drawers_scrapped` became optional (`None` default);
  `upsert_daily_summary` treats `None` as "leave whatever this date/shift already
  has alone" (0 for a brand new row) instead of zeroing out a legacy value just
  because the new form never sends the field. The MCP `record_daily_production`
  write tool and any direct API/script caller can still pass an explicit value.
- **New Defect form**: the Scrap disposition button is untouched — still a
  secondary/tucked-away option, unaffected by any of the above.

### Auto-calculation (Unique Drawers Rejected / Drawers Reworked)

`GET /api/v1/daily-production/{production_date}/suggested-counts` →
`{"production_date", "defect_case_count", "suggested_drawers_rejected_unique",
"suggested_drawers_reworked"}`. Read-only — it never writes to
`DailyProductionSummary`, so calling it (including via the Daily Summary form's
"Recalculate from defect cases" button) can never overwrite a saved entry.
Implemented in `app/services/defect_service.py suggested_daily_counts()`:

- **Unique drawers rejected** = count of distinct, non-deleted `DefectCase` rows
  for that `production_date`, regardless of disposition. One `DefectCase` already
  IS one defective drawer no matter how many `DefectItem` categories are on it
  (`PROJECT_SPEC.md` section 2), so counting distinct cases (not items) is the
  same dedup rule used everywhere else in the app (e.g.
  `app/routers/reports.py _distinct_cases`) and already guarantees a drawer
  flagged under two categories on one case is counted once, not twice.
- **Drawers reworked** = count of those cases with disposition `Rework` closed as
  `Closed - Repaired` — covers a case closed via the resolved-on-the-spot fast
  path, "Close Directly", or the legacy recheck path equally, since all three land
  on the same disposition/status combination (`PROJECT_SPEC.md` section 3.3).
- **Limitation**: `DefectCase` has no shift field, only `production_date`, so this
  suggestion is computed at the whole-day level — a plant running two shifts
  against the same production date would see the identical suggestion on both
  shifts' forms. Matching by shift would require adding a shift field to
  `DefectCase`, which is out of scope here.

The Daily Summary page loads whichever is true for the selected date/shift: if a
row is already saved, its saved values are shown as-is (auto-fill never runs);
otherwise the suggestion pre-fills the form for a brand-new entry. A manual edit
before saving is never overridden — the suggestion is only ever a starting point.
