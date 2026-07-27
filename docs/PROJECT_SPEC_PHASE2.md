# Phase 2 Addendum — Customer Issues Tab

This is an addendum to [`PROJECT_SPEC.md`](PROJECT_SPEC.md), not a replacement.
Everything in the original spec still applies unchanged. This document covers only
what Phase 2 added.

## Purpose

Eagle Woodworking's daily production brief already reads customer complaint emails
and classifies them (date, customer, order number, category, source type, which
station should have caught it, piece count, estimated rework cost, description,
photos). Phase 2 replicates that data model locally as a **Customer Issues** tab so
the dashboard can show external quality signal alongside internal QC data, before the
real brief system (Airtable or an API) is wired up directly.

## Scope and boundaries

- `CustomerIssue` / `CustomerIssueCategory` are **new, separate tables** — nothing in
  `DefectCase`, `DefectItem`, `DailyProductionSummary`, station/category master data,
  status transitions, or existing routes changed to build this.
- A customer issue may optionally be **linked** to an internal `DefectCase` via
  `linked_defect_case_id` once QC confirms the connection (sets `status = "Linked"`).
  Linking never creates/merges DefectItem rows and never affects internal defect
  counting — see `PROJECT_SPEC.md` section 2 for those rules, unchanged.
- Business rules live in `app/services/customer_issue_service.py`, kept deliberately
  separate from `app/services/defect_service.py` — same reasoning as the original
  spec's UI → API → service → DB layering, just for a second, independent data type.

## Data model summary

`CustomerIssueCategory`: id, name (unique), active, sort_order — same shape as
`DefectCategory`, seeded in this order: Wrong Size, Wrong Spec, Joinery, Finish
Quality, Missing Parts, Shipping Damage / Crushed Box, Corner Impact, Warp or Crack,
Hinge Holes, Other.

`CustomerIssue`: issue_number (`CI-YYYYMMDD-NNNN`, daily-reset sequence, same pattern
as `DefectCase.case_number`), reported_date, customer_name, order_number (nullable —
"order not identified" is a normal, expected state), issue_category_id, source_type
(`Manufacturing` | `Shipping Damage`), should_have_caught_at (free-text station hint,
e.g. "QA/Final"), piece_count (>= 1), estimated_rework_cost (auto-calculated as
`piece_count * $100` when not explicitly given), description, photo_urls
(comma-separated, for now), status (`Open` | `Ignored` | `Linked`),
linked_defect_case_id, notes, is_deleted (soft delete only, same policy as
`DefectCase`).

## KPI formulas (exact, used identically everywhere)

- **Customer Issues per Day** = count of customer issues in the period.
- **Estimated External Rework Cost** = sum of `estimated_rework_cost` in the period.
- **Escape Rate** = `(Customer Issues / Drawers Inspected) * 100`. Drawers Inspected
  comes from the same `DailyProductionSummary` rows the internal KPIs use. Null/"N/A"
  when Drawers Inspected is 0.
- **Internal Catch Rate** = `(Internal Defect Events / (Internal Defect Events +
  Customer Issues)) * 100`. Null/"N/A" only when **both** internal defect events and
  customer issues are zero for the period — if either is nonzero the rate is a real
  number (including a valid 0%).

## API

All under `/api/v1/customer-issues`, plus `GET /api/v1/exports/customer-issues.csv`.
Route registration order matters: `/categories`, `/summary`, and `/pareto` are
registered before `/{issue_id}` so they can never be shadowed by the parameterized
route. Pareto's default measure is **issue count** (not piece count), matching
"Customer Issues per Day" being a count — piece/cost totals are reported separately
in the summary endpoint.

## UI

New "Customer Issues" nav tab (`/customer-issues`), plus a small summary card on the
main Dashboard linking to it. Rows with no order number are visually flagged (not by
color alone — the badge carries the text "Order not identified"). Row actions: link
to an internal case, resolve order number, add notes, ignore (with confirmation).

## Demo data

`scripts/seed_customer_issues.py` generates synthetic, clearly-fake customer names
and 15-20 issues weighted toward Wrong Size / Wrong Spec / Finish Quality, mostly
Manufacturing source type, a mix of resolved/unresolved order numbers, and links a
couple to existing internal defect cases if any exist.
