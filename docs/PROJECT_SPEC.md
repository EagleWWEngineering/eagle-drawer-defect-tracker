# Eagle Drawer Defect Tracker — Project Specification

This is the full, durable specification for the Eagle Drawer Defect Tracker. `AGENTS.md`
and `CLAUDE.md` at the repo root hold a short summary for AI coding assistants; this
document is the source of truth they point back to.

## 1. Purpose

Eagle Woodworking produces custom dovetail drawer boxes. This is a **drawers-only pilot**
to capture defect, rework, scrap, and repair data in one durable dataset so:

- QC can record findings fast and route drawers through the correct repair priority
  without escalating every normal decision to the production manager.
- The Manufacturing Engineer gets daily data for Pareto analysis and root-cause work.
- The paper-first process still works: a daily paper log can be collected and entered
  into the app later the same day.

This is a **process-improvement tool, not an employee-performance ranking tool**.
Operator names are not collected. A suspected source station is never treated as
proven blame — it is a hypothesis field, kept separate from the station where the
defect was actually found.

## 2. Confirmed business rules (do not change without a deliberate decision)

1. Scope is drawer production only. No cabinet-door-specific features.
2. The required production identifier is **Work Order Number**. There is no separate
   required Job Number or Drawer ID. An optional **Drawer/Part Reference** field exists
   for future traceability but is never required.
3. One drawer with multiple physical defects in the *same* category = **one defect
   event** for that category.
4. One drawer with defects in *two different* categories = **two defect events**, but
   still **one defective drawer**.
5. Multiple drawers with the same category count once per affected drawer via an
   **affected drawer quantity** field on each defect item (default 1).
6. Reinspection failure for the same *unresolved* defect updates the existing case and
   its status history — it does not create a duplicate case. A newly confirmed category
   can be added to the same case.
7. QC and Notch & Bore are separate stations.
8. Work-order priority matters operationally (an urgent order finishing removes it from
   WIP sooner). Priority is always shown with a **text label**, never color alone.
9. **Found station** (where the defect was discovered) and **possible source station**
   (a hypothesis about where it originated) are different fields. The UI and API must
   never describe possible source station as a confirmed root cause.
10. Photos are optional.
11. Root cause and corrective action are optional fields, normally filled in after the
    initial defect entry, during investigation.
12. The Manufacturing Engineer needs CSV/Excel-compatible export of daily counts.

### 2.1 Counting definitions (used identically in API, dashboard, exports, MCP, docs, tests)

- **Defect Event**: one affected-drawer/category combination. Summed as the affected
  drawer quantity across defect items.
- **Defective Drawer**: one unique drawer with at least one category recorded against
  it. Because individual drawer IDs aren't always tracked, the daily unique
  defective/rejected-drawer count is entered directly on the Daily Production Summary.
- **Drawers Inspected**: unique drawers inspected in the period, from the Daily
  Production Summary.
- **Defects per 100 Drawers** = `(Defect Events / Drawers Inspected) * 100`
- **Drawer Rejection Rate** = `(Unique Drawers Rejected / Drawers Inspected) * 100`
- **First Pass Yield** = `((Drawers Inspected - Unique Drawers Rejected) / Drawers Inspected) * 100`
- **Rework Rate** = `(Drawers Reworked / Drawers Inspected) * 100`
- **Scrap Rate** = `(Drawers Scrapped / Drawers Inspected) * 100`

If Drawers Inspected is zero, **every rate is `null`**, displayed as `"N/A"`. Never
divide by zero.

### 2.2 Worked examples (also encoded as automated tests)

- One drawer, three sanding scratches → 1 Sanding defect event, 1 defective drawer.
- One drawer, Sanding + Dado defects → 2 defect events, 1 defective drawer.
- Three drawers, each with a Sanding defect → 3 defect events, affected drawer
  quantity summed to 3.

### 2.3 Open question tracked for the pilot (not blocking MVP)

Rework and scrap often happen on a **later date** than the original rejection, so a
single day's Daily Production Summary can legitimately show rework/scrap counts with
zero rejections that day. Until the pilot confirms how dates should be attributed
across a multi-day repair cycle, the following comparisons are **soft warnings that
require a note to override**, not hard blocks:
`reworked <= rejected_unique`, `scrapped <= rejected_unique`,
`reworked + scrapped <= rejected_unique`.
The only **hard** rule is `rejected_unique <= inspected`.

## 3. Master data (seeded, editable via Admin, never hard-deleted if referenced)

**Stations** (sort order 1–15):
Ripping & Picking, Cross Cut, Dovetail Machine, Dado, Assembly, Putty, Side Sanding,
Top Sanding, Seal Coat, Dry Time 1, Prep Sanding, Top Coat, Dry Time 2, Notch & Bore,
QC / Sorting / Shipping.

**Defect categories**: Bad Wood / Material; Cutting / Incorrect Dimension;
Dovetail / Machining; Dado / Bottom Groove; Bottom Panel;
Assembly / Joint / Glue / Staple; Putty / Surface Fill; Sanding / Surface;
Finish / Coating; Notch & Bore; Scoop / Custom Cutout; Damage / Handling;
Wrong Feature / Orientation; Other.

**Priorities**: Urgent (red), High (orange), Normal (blue) — always paired with text.

**Statuses**: Open, In Rework, Waiting, Ready for QC Recheck, Closed - Repaired,
Closed - Scrapped, Closed - Use As Is.

**Dispositions**: Rework, Scrap, Use As Is, Hold. **No "Remake" disposition in the
MVP** — the floor doesn't yet separate rework from remake, and the free-text
**repair action** field captures what actually happened (patched, reused parts, cut
new sides, etc.). Remake can be added post-pilot if the data supports it.

### 3.1 Status transition map (enforced in `defect_service.py`, one table/function)

```
Open                    -> In Rework, Waiting, Closed-Scrapped, Closed-Use As Is
In Rework                -> Ready for QC Recheck, Waiting
Waiting                  -> In Rework, Closed-Scrapped
Ready for QC Recheck    -> Closed-Repaired, In Rework, Closed-Scrapped, Closed-Use As Is
Any Closed status        -> Open only via explicit reopen (requires a note + audit entry)
```

### 3.2 Disposition → status routing

Rework → In Rework · Hold → Waiting · Scrap → Closed – Scrapped ·
Use As Is → Closed – Use As Is.

## 4. Architecture

- Python 3.11+, FastAPI, SQLite, SQLAlchemy 2.x + Alembic (`render_as_batch=True`),
  Pydantic v2, Jinja2 + plain HTML/CSS/JS (no frontend framework), a locally-bundled
  chart library, the official Python MCP SDK for a separate stdio server, httpx for
  the MCP server's HTTP calls, pytest, Ruff.
- FastAPI runs at `http://127.0.0.1:8000`. The MCP server is a separate stdio process
  reading `DEFECT_API_URL` (default `http://127.0.0.1:8000`).
- Layering: **UI** (forms/charts, JSON only) → **API routers** (HTTP I/O only) →
  **service layer** (all counting/validation/status-transition/reporting rules) →
  **database layer** (persistence only). **MCP server calls the same REST API** the UI
  uses — it never touches SQLite directly. This keeps one source of business logic
  for both the UI and the MCP path.
- stdio MCP servers must never write logs to stdout (it corrupts the protocol stream).
  Logs go to stderr or a file.

## 5. Data model summary

See `docs/DATA_DICTIONARY.md` for full field lists, constraints, and example JSON.
Key tables: `DailyProductionSummary`, `DefectCase`, `DefectItem`, `DefectPhoto`,
`StatusHistory`, `AuditLog`, `Station`, `DefectCategory`. Timestamps stored in UTC,
displayed in `America/New_York`. SQLite runs with foreign keys on, WAL mode, and a
busy timeout.

## 6. API

Versioned JSON under `/api/v1`, consistent success/error envelopes, no raw stack
traces returned to clients. Routes are listed in `docs/DATA_DICTIONARY.md` and appear
in the generated OpenAPI docs at `/docs`.

## 7. MCP server

Read tools: `get_defect_summary`, `get_defect_pareto`, `search_defect_cases`,
`get_rework_queue`, `get_work_order_defect_history`, `get_defect_case`.
Write tools: `record_defect_case`, `record_daily_production`,
`update_defect_case_status`. Resource: `quality://defect-tracker/data-dictionary`.
Prompt: `weekly_quality_review`. Full setup in `docs/MCP_SETUP.md`.

## 8. Safety and reliability

Binds to `127.0.0.1` only. No production auth in the MVP (any role selector is
labeled a prototype). Soft delete only for defect cases. Every create/edit/status
change/soft delete/export/master-data change/MCP write is audited. Photo uploads are
validated by MIME type, extension, size, and filename safety. No customer PII or
credentials are stored. All user text is HTML-escaped; all DB access is
parameterized through SQLAlchemy. A timestamped SQLite backup script uses the
official online backup API. The app works fully offline after install.

## 9. Reports and Pareto

Default Pareto measure is **Defect Events** (not unique drawers), sorted highest to
lowest, with running cumulative percentage. Chart totals must match the filtered
record total exactly. Selecting a Pareto bar drills into the underlying cases.
Possible source station is never labeled a root cause. Displayed rates round to one
decimal place; stored/calculated values keep full precision. CSV export includes raw
counts and identifiers, not just percentages.

## 10. Acceptance criteria

All automated tests pass; Ruff passes; migrations apply cleanly to an empty database;
the app starts with one documented command; the core workflow (dashboard → new
defect → daily summary → rework queue → reports → export → MCP query) works visually
at desktop and mobile widths with no overflow; README/USER_GUIDE/LEARNING_GUIDE/
DATA_DICTIONARY/MCP_SETUP are complete; no TODO placeholders remain in required
functionality.
