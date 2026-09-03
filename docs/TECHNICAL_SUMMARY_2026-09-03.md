# Eagle Drawer Defect Tracker — Technical Summary (2026-09-03)

Complete, standalone snapshot of the app as it exists right now. This document does not
assume the reader has seen any prior summary — everything needed to understand the whole
system is below. It supersedes `docs/TECHNICAL_SUMMARY_2026-09-01.md`, which is left in
place, untouched, for historical reference.

**Verified by:** `git log --oneline 867e6a3..b88dd66` and `git diff --stat 867e6a3..b88dd66`
(867e6a3 is the commit the 09-01 summary was written against; `b88dd66` is current
`origin/master`, confirmed via `git log --oneline -1` and cross-checked against the known
production Alembic head below) — 47 files changed, 17,105 insertions, 124 deletions, across
14 commits. That range turned out to hold more than the 8 commits this task was framed
around: it also contains **Phase 9** (work order line + label scanning, plus five same-day
hotfixes — `4463d2b` through `0bf4d00`), which the 09-01 summary itself had explicitly
described as *untracked, uncommitted, and not part of the running app*. That description is
no longer accurate — Phase 9 fully landed as committed code before this session's own work
began, and is described in full below, not carried forward from the stale claim. Every file
in the diff was either read fresh this session or confirmed absent from the diff (and
therefore byte-identical to what the 09-01 summary already verified). The real test suite was
run (`./.venv/Scripts/python.exe -m pytest -q`) — **676 passed** (391 unit, 269 api, 16 mcp),
up from 534 at the time of the 09-01 summary — along with `ruff check .` (clean) and
`ruff format --check .` (clean, no exceptions this time). `alembic heads` reports
`4e9b5dea94ec` as the sole head, 12 migration files total (up from 9). Anything not
confirmable from this sandbox (no Render API/CLI access, no login credential, no production
database access) is marked **NOT VERIFIED**, except the specific facts under "Known production
state" below, which Rodolfo verified directly in Render Shell today and are reported on that
authority.

**Known production state** (verified by Rodolfo in Render Shell, 2026-09-03 — not verified by
this sandbox):
- Production's Alembic revision is `4e9b5dea94ec (head)` — matches this sandbox's local head
  exactly; every migration described in this document has been applied in production.
- `FAVORITES_ENABLED=true` is set directly in the Render environment (not via `render.yaml` —
  see §7); favorites are live and in use on the shop floor.
- Database backups exist at `/var/data/backups/`, including
  `defect_tracker_20260903_pre_seedkey.db` (12M), taken before the seed-key migration ran.
- The seed-duplicate incident described in §3 is confirmed to have occurred in production.

---

## 1. Architecture & stack

- **Language/framework:** Python ≥3.11, FastAPI 0.115.6, served by Uvicorn 0.34.0.
- **Database:** SQLite, accessed through SQLAlchemy 2.0.36 (declarative `Mapped[...]` style
  models). WAL journal mode, foreign keys **ON**, 15s busy timeout — confirmed directly in
  `app/database.py`'s `_set_sqlite_pragma` (`PRAGMA foreign_keys=ON`, `PRAGMA
  journal_mode=WAL`, `PRAGMA busy_timeout=15000`, run on every new connection via a SQLAlchemy
  `connect` event). The test suite's own engine fixtures set the same pragma independently.
- **Migrations:** Alembic 1.14.0. **Twelve** migration files exist (three more than the
  09-01 summary's nine — see §10 for the full ordered list): `line_label`/`entry_source` on
  `defect_cases` (Phase 9), `is_favorite`/`favorite_rank` on `stations`/`defect_categories`
  (Phase 3, this session), and `seed_key` on the same two tables (this session's
  seed-duplicate fix). `alembic upgrade head` applies cleanly to a fresh database — verified
  this session by running it against a from-scratch SQLite file.
- **Templating/frontend:** Server-rendered Jinja2 (3.1.5) HTML pages, plain vanilla JS/CSS —
  no frontend framework/bundler. One vendored client-side library newly relevant this
  session: **Tesseract.js** (OCR, §3) alongside the already-vendored **jsQR** (QR decode).
- **Other real dependencies:** `httpx` (outbound HTTP — production-brief sync, relay
  scripts), `mcp` 1.28.1 (the MCP server), `bcrypt` 4.2.1 (login password hashing),
  `python-dotenv` (loads `.env` locally). New this session: **Playwright** 1.62.0, declared
  as an optional `e2e` dependency group in `pyproject.toml` (separate from `dev` — it needs a
  `playwright install chromium` step beyond `pip install`), added specifically because
  pytest's in-memory SQLite test database has been shown twice this session to hide real
  bugs that only a genuine running server reproduces (§8).
- **Layering:** UI page routes and JSON API routers (`app/routers/*.py`, HTTP input/output
  only) → service layer (`app/services/*.py`, all business rules and DB queries) → SQLAlchemy
  models (`app/models.py`, persistence only, no rules). Routers never contain business logic;
  services never touch `Request`/`Response` objects. This discipline is enforced by
  convention/code review, not a technical barrier. One new service file this session,
  `app/services/master_data_service.py` — station/category field edits (name/active/
  sort_order) and the new favorites max-5 enforcement, previously inline in the router,
  moved here together so the router stays thin and the one real business rule (the cap)
  isn't bypassable by calling the API directly.
- **MCP server** (`mcp_server/server.py`): a separate stdio process (started by Claude
  Code/Codex, not the web app) exposing the same functionality as MCP tools. It calls the
  REST API over HTTP (`DEFECT_API_URL`) exactly like the browser UI does — it never touches
  the SQLite file directly. Unchanged this session (confirmed: absent from the diff).
- **Hosting (Render):** one `web` service, `plan: starter` (persistent disks require a paid
  plan). Build: `pip install .`. Start: `alembic upgrade head && uvicorn app.main:app --host
  0.0.0.0 --port $PORT`. A 1GB persistent disk mounted at `/var/data`, holding
  `/var/data/defect_tracker.db` (`DATABASE_URL`) and `/var/data/uploads/` (`UPLOADS_DIR`).
- **`app/routers/scan.py` is now imported and registered** in `app/main.py` (`from app.routers
  import (..., scan, ...)`, `app.include_router(scan.router)`) — the 09-01 summary described
  this exact import as having previously crashed the Render deploy and been removed; Phase 9
  re-added it correctly, with the feature fully built out this time (§3), and it is confirmed
  live and working (676 passing tests exercise it, including live-server checks this session
  for unrelated features running on the same app instance).

---

## 2. Data model

All tables currently defined in `app/models.py` — still **13 total**, no table added or
dropped since 09-01; three tables gained columns.

| Table | Purpose | Key fields |
|---|---|---|
| `stations` | Production steps (e.g. "Dado", "QC / Sorting / Shipping") | `name` (unique), `active`, `sort_order`, **`is_favorite`, `favorite_rank`** (nullable int, only meaningful while favorited — new, Phase 3), **`seed_key`** (nullable, new — see §3's seed-duplicate section) |
| `defect_categories` | Internal defect classifications | same shape as `stations`, including the same three new columns |
| `customer_issue_categories` | Customer-complaint classifications (deliberately separate vocabulary from `defect_categories`) | same shape as `stations`, **unchanged** — no favorites/seed_key columns here; the identical seed-duplicate exposure exists on this table too but was deliberately not fixed this session (§3, §8) |
| `daily_production_summaries` | One row per (`production_date`, `shift`) — the denominators for every rate | `drawers_inspected`, `drawers_rejected_unique`, `drawers_reworked`, `drawers_scrapped` (kept for backward compatibility, no longer drive any KPI), `cost_per_drawer_at_time` (nullable historical snapshot, not read for cost — see §3). Unique on (`production_date`, `shift`). |
| `daily_schedules` | One row per calendar date: how many drawers the production brief scheduled that day | `production_date` (primary key), `drawers_scheduled`, `source` (`"sync"`/`"manual"`), `synced_at`, `updated_at`. Manual-wins rule — see §6. |
| `defect_cases` | One QC finding for one work order — the header record | `case_number` (unique, `DF-YYYYMMDD-NNNN`), `found_station_id`/`possible_source_station_id` (FKs to `stations`), `priority`, `status`, `disposition`, `resolved_on_the_spot`, `skipped_recheck` (retired), `cost_per_drawer_at_time` (nullable snapshot), `closed_at`, `is_deleted`, **`line_label`** (nullable `String(10)`, new — Phase 9), **`entry_source`** (nullable `String(20)`, `"manual"`/`"scanned"`/`"scanned_edited"`, new — Phase 9). New composite index `ix_defect_cases_work_order_line` on (`work_order_number`, `line_label`) — not unique; one line legitimately appears on many cases. |
| `defect_items` | One category on one case, with an affected-drawer quantity | FK to `defect_cases` (`ondelete="CASCADE"`) + `defect_categories`; unique on that pair (duplicates are merged, never a second row) |
| `defect_photos` | Metadata for a photo attached to a case (file lives on disk at `uploads_dir/stored_filename`) | `stored_filename`, `original_filename`, `content_type`; FK to `defect_cases` (`ondelete="CASCADE"`) |
| `status_history` | Audit trail of every status change on a case | `from_status`, `to_status`, `note`, `changed_at` |
| `audit_log` | Append-only log of every create/edit/status-change/delete/export/MCP-write | `actor_role`, `action`, `entity_type`, before/after JSON. New `action` values this session: `item_add`/`item_update`/`item_remove`/`photo_delete` (§3) — same `audit_service.record()` call, no schema change, no second logging mechanism. |
| `customer_issues` | A customer complaint, synced from the production brief or entered manually | `issue_number` (unique), `source_thread_id` (unique, nullable — sync dedup key), `linked_defect_case_id`, `status` (Open/Ignored/Linked) |
| `sync_logs` | One row per production-brief sync attempt, success or failure | `source_url`, `records_fetched/created/updated/skipped`, `errors`, `status` |
| `auth_sessions` | A server-side login session — no `expires_at`/TTL column at all | `token` (unique), `created_at` (informational only) |
| `app_settings` | Generic key/value store, no migration needed per new setting | `key` (primary key), `value` (string), `updated_at` |

No column has been dropped. `daily_production_summaries.drawers_reworked`/`drawers_scrapped`/
`cost_per_drawer_at_time` and `defect_cases.skipped_recheck` all still exist with their
historical values fully intact. `app/routers/master_data.py` intentionally has no `DELETE`
route for stations or defect categories — records may be deactivated but never hard-deleted
if referenced by historical data (see the seed-duplicate section below for why this matters
more than it might look).

**`app_settings` rows in use today:** `auth_username`/`auth_password_hash` (§5),
`cost_per_drawer` (§3), `sync_manual_requested_at`/`sync_relay_last_seen_at` (§6, Customer
Issues relay only). Favorites' state lives directly on `stations`/`defect_categories`
(`is_favorite`/`favorite_rank`), not here.

**Relationships:** `defect_cases` → `defect_items`/`defect_photos`/`status_history` (cascade
`delete-orphan` — the *only* direction this cascade runs; deleting one `DefectItem` or one
`DefectPhoto` directly never cascades to anything else, confirmed by re-reading every
relationship in `app/models.py`: nothing anywhere has a foreign key into `defect_items` or
`defect_photos`, so nothing can cascade off either of them). `defect_items` → `defect_categories`.
`customer_issues` → `customer_issue_categories` and optionally → `defect_cases`.
`daily_schedules` has no foreign keys, keyed purely by `production_date`.

### Master data: active-only filtering and Favorites (new this session)

`GET /api/v1/master-data` (`app/routers/master_data.py`) gained an `active_only` query
parameter, default `false`. When `true`, both the `stations` and `defect_categories` lists in
the response exclude inactive rows; when omitted (every existing caller — Admin, Dashboard,
Reports, Rework Queue), the response is unfiltered, exactly as before. Only the New Defect
form's own JS passes `true`. Root cause this fixed: previously there was no way to ask for an
active-only list at all, so deactivating a station/category in Admin never removed it from
the New Defect form's choices.

The response also carries `favorites_enabled: bool` (mirrors `Settings.favorites_enabled`,
§7's kill switch) and each `StationOut`/`DefectCategoryOut` row now includes `is_favorite`,
`favorite_rank` (nullable int), and `created_at`/`created_at_local` (the last two are Admin-
display-only, read-only, added for the seed-duplicate investigation — §3).

**Favorites**, enforced in `app/services/master_data_service.py`:
- Up to 5 stations and, independently, up to 5 defect categories may be marked `is_favorite`.
  Setting a 6th on either table is rejected outright (`ValidationError`, 400, message
  "Already at 5 favorited stations - unfavorite one first" or the category equivalent) —
  never a silent bump of the oldest favorite.
- The 5-cap counts **active** favorited rows only. A station stays flagged `is_favorite` while
  deactivated (so reactivating it later brings it straight back to the quick-pick bar with no
  re-favoriting step), which means an inactive-but-favorited row does not consume one of the
  5 slots.
- `favorite_rank` is auto-assigned (first free slot, 1–5) at favorite-time — not client-
  settable. Manual reordering (drag/up-down) was scoped out as a deliberate fast-follow, not
  built this phase.
- Unfavoriting leaves `favorite_rank` as-is (not cleared) — a deliberate choice so re-
  favoriting later, without a fresh explicit rank choice, can just re-take its old slot if
  still free.
- An inactive station/category, even if still flagged `is_favorite`, is excluded from the
  active-only endpoint and therefore never appears in the New Defect form's favorites bar —
  "active status still wins."

**Admin** (`app/templates/admin.html`) gained, per station/category row: the Favorite
checkbox above (hidden entirely — the whole column, not just the checkbox — when
`FAVORITES_ENABLED` is off), a live "X/5 favorited" count next to each section heading, and a
read-only **Created** column (`created_at_local`), added specifically so a stray
seed-duplicate row's later creation timestamp stands out against the rows created at initial
go-live (see the seed-duplicate section below). Saving a row now reloads the whole table from
the server afterward — previously it sat stale showing pre-save values until a full page
reload.

**On the New Defect form**, when `favorites_enabled` is true and at least one favorite exists
for that table: the Found Station and Possible Source quick-pick bars (both already existed
as a "common stations" tier above a collapsed full list) are populated from the *same*
favorited-stations set (favorites are per-station, not per-field) instead of the old
hardcoded `COMMON_STATION_NAMES` list — a replacement of that tier's content, not a third
tier. A separate, brand-new favorites bar sits above the full defect-category grid; the full
grid itself was never collapsed and stays exactly as visible as before — deliberately, since
collapsing it would add a tap for every category outside the top 5 and change a workflow
already in daily shop-floor use. When the kill switch is off, or a table has zero favorites
configured, both bars fall back to today's original behavior exactly — verified live this
session in both directions (favorites on with 5 configured, and favorites off against the
same database) via a headless-browser walkthrough.

---

## 3. Core business rules

### Status/disposition vocabulary, cost model, counting rules (Phase 7, unchanged)

*(Confirmed unchanged — `app/services/defect_service.py`'s status/disposition/cost machinery
and `app/services/metrics_service.py`'s KPI formulas are absent from the `867e6a3..b88dd66`
diff except for one additive parameter described below. Carried forward from the 09-01
summary, re-verified this session by re-reading the current file, not merely trusted.)*

```python
# app/services/defect_service.py
VALID_STATUSES = ["Open", "Closed - Repaired", "Closed - Use As Is"]
RETIRED_STATUSES = ["In Rework", "Waiting", "Ready for QC Recheck", "Closed - Scrapped"]
ALL_KNOWN_STATUSES = VALID_STATUSES + RETIRED_STATUSES   # display/filter only

VALID_DISPOSITIONS = ["Rework", "Set Aside"]
RETIRED_DISPOSITIONS = ["Use As Is", "Hold", "Scrap"]
ALL_KNOWN_DISPOSITIONS = VALID_DISPOSITIONS + RETIRED_DISPOSITIONS   # display/filter only
```

`STATUS_TRANSITIONS` maps **every** status (including every closed one) to an empty set —
there is no generic "move to an intermediate status" transition at all. Exactly two kinds of
transition exist:

- **Direct close** (`direct_close_statuses()`): every non-closed status can close straight to
  `Closed - Repaired` or `Closed - Use As Is` (`NEW_CLOSE_STATUSES`), with an optional note.
- **Reopen**: a closed case (any of the three closed statuses) can move back to `Open`. This
  is handled entirely as a special case inside `update_case_status`'s `is_reopen` branch
  (`current_status in CLOSED_STATUSES and new_status == "Open"`) — it is **not** part of
  `allowed_next_statuses()`, which stays empty for closed statuses like everything else.
  Reopening is the one transition that always **requires** a note. Until this session, the
  only way to trigger it was a direct API call — no UI anywhere exposed it. See "The
  unreachable-reopen gap" below.

**"Fixed immediately?" fast path** (`create_defect_case`): only `disposition="Rework"` has an
instant-close path, requiring a `repair_action`. `instant_close_outcome` picks the terminal
status: `"Repaired"` (default) or `"Use As Is"`.

**Cost model:** one cost unit snapshotted per `DefectCase` at creation
(`cost_per_drawer_at_time`), never derived from `DailyProductionSummary`, never multiplied by
`affected_drawer_quantity`. A case closed `"Closed - Use As Is"` contributes zero to Internal
Rework Cost and its unit to Cost Avoided instead.

**Rework Rate:** count of distinct, non-deleted cases with `disposition == "Rework"` in the
filtered range, no status qualifier.

**Defect event counting:** one category logged on one case = one defect event, regardless of
physical defect count within that category; duplicate categories submitted together on one
case are merged (quantities summed), enforced by both application logic and a DB unique
constraint on (`defect_case_id`, `defect_category_id`).

**Scrap removal (Phase 4, still in effect):** no Scrap Rate/Internal Scrap Cost anywhere in
this app.

### Work order line + label scanning (Phase 9 — new since the 09-01 summary)

The New Defect form's Work Order Number field has a "📷 Scan label" button next to it, and a
separate two-character **Line** field. Tapping Scan opens a camera modal that runs two
independent reads of the same physical drawer label simultaneously:

1. **QR code → the 6-digit work order number.** Decoded client-side (native
   `BarcodeDetector` where supported; the vendored `jsQR` library as an iOS Safari fallback).
   Free, exact, works even with OCR fully disabled.
2. **Printed text → the work order line letter(s) + dimensions**, via OCR. The default engine
   is **Tesseract.js, running entirely in the browser tab** — not a server round-trip. Only
   the already-recognized text is ever sent to the server (`POST /api/v1/scan/parse-label`),
   never an image, in this default path.

As soon as the QR decodes, the order number auto-fills. Once OCR resolves, the Line field
fills only on a confident single-letter result; up to 3 lower-confidence "alternates" render
as tappable buttons. If nothing confident came back (bad photo, OCR disabled, low confidence,
or the OCR-read order number disagreeing with the QR's), the flow ends at a **required
manual letter picker** (A–Z, plus an AA–ZZ toggle) — a failed scan is never a dead end, and
manual typing always works regardless of camera/OCR/QR outcome; scanning only ever fills form
inputs via callbacks, it never submits the form itself.

`entry_source` (stored per case) tracks provenance of the Line field specifically: starts
`"manual"`, becomes `"scanned"` only on a trusted OCR read, and flips to `"scanned_edited"`
the moment the operator changes it by hand after a scan (including tapping an alternate or
using the manual picker post-scan) — so a field that's frequently `"scanned_edited"` in
practice is a direct signal that this feature's parsing needs work for that pattern of label.

`normalize_line_label()` (`app/services/defect_service.py`) uppercases, strips whitespace,
and turns blank into `None` — it never rejects or validates content, so a scanned/typed line
never blocks submission. `list_cases` supports filtering by `line_label` (normalized the same
way before comparison); `GET /api/v1/reports/work-orders/{wo}` returns a per-line breakdown
(`WorkOrderHistoryOut.by_line`) grouping that work order's cases by line, `None`-group first.
The CSV export gained its own `line_label` column (blank, not a dash, when absent).

**Server-side pipeline** (`app/services/ocr_service.py`, `app/routers/scan.py`, three
endpoints — `GET /config`, `POST /parse-label`, `POST /diagnose`, none of which take a
database session, deliberately, so scanning can never write to the database): a genuinely
functional geometry/parsing pipeline, not a stub. Regex-based field extraction (order number,
dimensions, thickness, a "corner block" thickness|height pair tolerant of the separator being
OCR-misread as `I`/`l`/`L`/`!`), a decoy-line exclusion list (lines mentioning "Bot:",
"ears", "lips" etc. never become the drawer's own dimensions), and line-label candidate
ranking that differs by path: the default browser path ranks candidates by distance to a
browser-computed expected corner (within 2.5× QR-size, confidence ≥65, else no candidate at
all — never a guessed letter); the optional cloud-provider path (Azure/Google/Anthropic
Claude, all off by default) ranks by distance from the QR instead, since it has no corner
geometry to work with. Cross-field validation checks the OCR order number against the QR's,
discarding the line-label read entirely if they disagree by 2+ digits (a 1-digit difference
is tolerated as OCR noise). Every validation check that couldn't run is recorded as *skipped*,
never conflated with a pass.

**Client-side** (`app/static/js/label-scan.js`): derives the QR's own rotated coordinate
frame from its corners, walks outward along that frame in small steps looking for a sustained
run of wood-colored pixels to confirm the label's true physical edge (falling back to a
QR-extrapolated fixed box if no edge is confirmed), warps the result into a normalized
canvas, then runs three sequential (never concurrent) Tesseract passes with different page-
segmentation modes: a whole-label pass (what actually reads the line label), a sparse-text
pass over the expected corner, and a digit-only dimension-crop pass. All recognized text is
converted to a common shape and POSTed to `/parse-label`; the client never re-implements the
server's ranking logic.

`OCR_ENABLED`/`OCR_PROVIDER`/`OCR_ENDPOINT`/`OCR_API_KEY` (`app/config.py`) are genuinely
wired up and consumed by `app/routers/scan.py` — contrary to the 09-01 summary's "inert
scaffolding" characterization, which was accurate *at the time* but predates Phase 9 actually
shipping. `OCR_ENABLED` now defaults **true** (the only optional feature in this app that
does) because the default `tesseract` engine runs entirely client-side, costs nothing, and
needs no credential; only switching to a cloud provider makes `OCR_API_KEY` matter, and the
`/diagnose` route still 503s without one.

**Five same-day hotfixes** (`7555b60` → `0bf4d00`, all 2026-09-01) iterated this feature
against real printed labels and a real camera, each fixing a concrete failure the previous
version's own diagnostic surfaced: a modal-close/controller-binding bug; a wrong Tesseract
page-segmentation mode for isolated 1-2-character text plus an overly strict order-number
mismatch check; corner-cropping abandoned entirely in favor of one generous whole-label pass
after real labels showed the corner-block text sitting immediately beside the line letter
with no safe crop boundary between them; ranking switched from "furthest from the QR" to
"nearest an expected corner" after the former kept selecting fragments of unrelated printed
words; and finally the boundary-walk's edge-detection check replaced with a wood-color-cast
test after a real photo showed printed ink being mistaken for the label's physical edge.

**Documented limitations, quoted from the code itself:** the line-label confidence floor
(65.0) and distance cap (2.5× QR-size) are explicitly noted as "chosen deliberately on the
strict side... not calibrated against a real label" beyond the handful of real photos used to
diagnose the five hotfixes; the wood-background edge check is explicitly untested against a
non-wood surface or any label but the one real photo used to build it; there is no JS test
framework in this repo, so every hotfix's client-side logic was verified via disposable,
uncommitted Node.js simulation harnesses, not an automated, repeatable test suite. See §8.

### DefectCase editing (Phase 2, this session) — found station/source, defect items, photos

`PATCH /api/v1/defect-cases/{id}` already accepted `found_station_id`/`possible_source_station_id`
in its update payload, but previously applied them via a blind `setattr` with **no existence
check at all**. This session added validation (existence only, not active-only — an edit must
still be able to keep or assign a station that's since gone inactive, matching how case
creation already worked) via a new `apply_case_field_updates()` in `defect_service.py`.
Editing either field has no effect on `cost_per_drawer_at_time` (verified by test).

**Defect items** — new endpoints: `POST`/`PATCH`/`DELETE
/api/v1/defect-cases/{id}/items[/{item_id}]`, adding, editing (quantity/notes), and removing
individual line items on an existing case. Adding merges into an existing item on the same
category, same rule as at case creation. Removing the last item on a case is blocked (a case
must always have ≥1 item, the same rule `DefectCaseCreate` already enforces at creation).

**Item add/edit/remove is blocked on a closed case**, a deliberate decision (not left
implicit): item counts feed Pareto/defect-event/rejection-rate figures with no status filter,
so editing items on an already-closed case would silently shift historical numbers for a day
already reported as final. The block raises `InvalidTransitionError` (a clean 400, not a
generic validation error) with a message telling the caller to reopen the case first. Found
station/possible source/root cause/photo edits are **not** gated this way — none of them feed
a counting formula.

**Photo deletion** — new `DELETE /api/v1/defect-cases/{id}/photos/{photo_id}`, removing both
the `DefectPhoto` row and the file under `UPLOADS_DIR` (best-effort file unlink — a missing or
failed file delete never blocks the row removal that already succeeded). Not gated by case
status.

Every one of these paths audit-logs through the existing `audit_service.record()` pattern —
new `action` values (`item_add`, `item_update`, `item_remove`, `photo_delete`), same
mechanism, no second logging system introduced.

**Cascade safety, confirmed by re-reading every relationship in `app/models.py`:** deleting
one `DefectItem` or one `DefectPhoto` is single-record only. Cascade (`delete-orphan`) exists
solely on `DefectCase.items`/`.photos` — the case→children direction — and fires only when
the *case* is deleted or an item/photo is removed from the case's own in-memory collection,
never the reverse. Nothing else in the schema holds a foreign key into `defect_items` or
`defect_photos`, so nothing can cascade off either table regardless.

### The unreachable-reopen gap (found and fixed this session)

The item-edit lock above shipped with a hint pointing the user at "Rework Queue → More
options → Status" to reopen a case. That path did not exist, for two independent, compounding
reasons:

1. **Rework Queue never lists closed cases.** `GET /api/v1/rework-queue`
   (`app/routers/reports.py`) defaults to the status set in `DIRECT_CLOSE_SOURCE_STATUSES`
   (the same "actionable/open" set `direct_close_statuses()` uses, shared deliberately)
   whenever no `status` query param is passed; a `status` param, if given, is used verbatim
   instead. The page itself (`rework_queue.html`) never sends a closed value — its own Status
   filter dropdown only ever offers two hardcoded options, "All open statuses" and "Open" —
   no closed-status option exists anywhere in it, so a closed case can never appear as a row
   there through the UI, in practice always.
2. **Even if one did appear, its "More options → Status" dropdown structurally could not
   offer "Open."** That dropdown's options come from `[item.status, ...item.allowed_next_statuses]`,
   and `allowed_next_statuses()` is an empty set for every status, closed ones included, by
   the Phase 7 design described above. A stale code comment on that dropdown claimed
   otherwise — it was wrong, and it is what pointed the original hint at this dead end.

Net effect: there was no reopen control anywhere in this app's UI at all. The only way to
reopen a case was a direct API call — exactly what this session's own Phase 2 tests did,
which is why they never caught the gap (see §8).

**Fixed** (`b88dd66`): a real Reopen button on the case-detail modal, next to the item-edit
hint. Clicking it reveals a required note field (reopening already requires one, enforced
server-side); submitting calls the existing status endpoint with `{new_status: "Open", note}`
and re-renders the modal, unlocking item editing immediately. No new endpoint was needed —
reopening was always fully supported server-side, just never exposed. The misleading hint
text and the stale rework-queue comment were both corrected in the same commit. Rework
Queue's own inability to list/browse a closed case is a real, separate gap, deliberately left
as a follow-up rather than bundled into this fix.

**Verified live with Playwright**, not just at the API level: created a case, closed it via
Rework Queue's own Close form, confirmed it drops out of the (open-only) queue listing,
confirmed the corrected hint text and locked item controls, reopened via the actual new
button (an empty-note submit attempt was correctly blocked by the browser's own required-field
validation), confirmed items unlocked, added an item, re-closed via Rework Queue, and
confirmed the lock re-engaged. Zero console errors throughout.

### The seed-duplicate incident (found, partially fixed, confirmed in production)

**Root cause:** `app/seed_data.py`'s `seed_master_data()` runs on **every** app startup (`app/
main.py` lifespan, not first-run only — this was already true before this session and is
unchanged) and decided "this default station/category already exists" purely by matching the
row's *current* `name` against the hardcoded default list. Renaming a row away from its
default name makes that name vanish from the check, so the very next restart silently
re-inserts the original default name as a brand-new row — not a revert of the rename, a
silent duplicate.

**What happened in production, confirmed:** fired on 2026-09-03. Roughly 12 duplicate defect
categories (and several duplicate stations) were created under their original default seed
names, alongside the admin's already-renamed rows. They were created **active**, so they
appeared as valid choices on the New Defect form — meaning a defect logged during that window
could have landed on the duplicate rather than the intended renamed category, splitting that
defect type's Pareto count across two rows.

**Cleanup performed by Rodolfo, manually, via Admin:** the duplicates were **deactivated, not
deleted**, and their sort order moved to 30–40 so they sink to the bottom of every list.

**Why deletion was never an option:** `Station.name`/`DefectCategory.name` are `unique=True`
at the database level, so a duplicate and its renamed counterpart are two rows with two
distinct names — no constraint violation, and nothing to detect by querying for a name
collision. Separately, `PRAGMA foreign_keys=ON` is confirmed enabled (§1), so any row a real
defect item/case already references cannot be deleted at all, only deactivated — the same
rule that already governed every other station/category in this app.

**Fix, Step 1 (shipped, `e85b362`):** an additive migration (`4e9b5dea94ec`) adds a nullable
`seed_key` column to both tables — the *original* default name a row was created under, set
once at insert time, never touched by an Admin edit. The seed loop's match is now "current
name already present" **OR** "seed_key already present." The name check is deliberately kept,
not replaced — switching to a seed_key-only check would mean an already-deactivated stray
duplicate (which is never backfilled a `seed_key`, by design — see below) looks "unseeded" to
the loop, triggering an insert attempt against a name the unique constraint would then reject
outright, crashing the next startup. Verified against a from-scratch reconstruction of the
real incident (a renamed row plus a raw-SQL-inserted, then-deactivated duplicate, migrated,
then re-seeded for real): no third row is ever created, and neither existing row's name,
active flag, or seed_key changes at all.

**The fragility that remains, stated plainly:** the migration's backfill touches only
currently-*active* rows whose name still matches a default — every renamed row, and every
already-deactivated stray duplicate (including this incident's ~12), gets `seed_key = NULL`
and stays there permanently, by explicit instruction, pending a human review of the real data
first. This means, right now, **the only thing preventing a third row from being created for
each of those ~12 default names is the continued existence of the deactivated duplicate row
holding that exact name** — the plain name-check, not seed_key. If one of those specific
inactive rows were ever hard-deleted (there is no path to do this through the app itself, but
direct database access could), the very next restart would recreate a fresh duplicate under
that name, with no rename required to retrigger it. `CLAUDE.md` was updated with an explicit
warning to this effect, next to the existing "master data is deactivated, never hard-deleted"
rule.

**A more durable Step 2 was discussed and deliberately deferred:** backfilling `seed_key` onto
the *renamed* rows instead (so the protection lives on the living row, not a dead one) would
require an explicit, human-confirmed `{renamed row → original default name}` mapping — not
safely inferable from the data, and a wrong mapping would manufacture a fresh instance of this
same bug for whichever default name got mapped incorrectly. `audit_log` likely holds this
mapping already for any rename done through the app's own Admin UI (every such rename is
audit-logged with before/after JSON), making it probably reconstructable rather than pure
guesswork — but this investigation was explicitly parked, not built, pending Rodolfo's review
of the real production data.

**`customer_issue_categories` has the identical seed-loop shape and the identical
theoretical exposure** — confirmed by re-reading `seed_master_data()`'s third loop, which
still matches purely by current name with no `seed_key` equivalent. Deliberately not fixed
this session; flagged here and in the `4e9b5dea94ec` migration's own docstring as a separate,
not-yet-decided follow-up.

---

## 4. KPI formulas

*(Unchanged in substance — `app/services/metrics_service.py`'s `compute_kpis()` and every
formula in it are absent from the `867e6a3..b88dd66` diff. Carried forward from the 09-01
summary, re-verified this session by re-reading the current file rather than trusted.)*

All `None`/"N/A" whenever `drawers_inspected == 0`: Defects per 100 Drawers, Drawer Rejection
Rate, First Pass Yield, **Rework Rate** (count of distinct, non-deleted cases with
`disposition == "Rework"` in the filtered range, no status qualifier), Total Internal Quality
Cost, Cost Avoided (what each case closed `"Closed - Use As Is"` would have cost had it not
shipped as-is), Schedule Attainment % (`total_inspected / total_scheduled * 100` over working
days only), and % Resolved On The Spot.

**Internal Quality Cost model:** one cost unit per `DefectCase` in the filtered range, using
that case's own snapshotted `cost_per_drawer_at_time` (or the currently-configured rate as a
fallback for a case predating that column). Never multiplied by `affected_drawer_quantity` or
`DefectItem` count. A case not closed `"Closed - Use As Is"` contributes its unit to Internal
Rework Cost and zero to Cost Avoided; a case closed `"Closed - Use As Is"` contributes zero to
Internal Rework Cost and its unit to Cost Avoided instead.

**The one real change since 09-01:** `filtered_defect_items_query()` — the shared row-fetch
function backing KPIs, Pareto, Trend, and CSV export — gained an optional `line_label`
parameter (Phase 9). When supplied, it filters `DefectCase.line_label ==
normalize_line_label(line_label)`, so a line-filtered KPI/Pareto/Trend/CSV total can never
silently disagree with a line-filtered record list. No formula itself changed.

There is still no scrap rate/cost anywhere in this app.

---

## 5. Authentication system

*(Unchanged — `app/services/auth_service.py` and `app/auth_middleware.py` are both absent
from the `867e6a3..b88dd66` diff. Carried forward, re-confirmed via that diff rather than
assumed.)*

Single shared login for the entire app — no per-user accounts, no roles (the cosmetic "Role
(prototype)" header selector is a separate, unauthenticated label used only to tag the audit
log; it is not part of this login).

**Credential flow:** `APP_USERNAME`/`APP_PASSWORD_HASH` originate as environment variables,
but the value actually checked at login time lives in the `app_settings` table, kept in sync
with the environment on every app startup.

**Sessions** are rows in `auth_sessions`, with no `expires_at`/TTL column — a session is valid
for as long as its row exists. This is what makes "Log out" (delete one row) and "Log out
everywhere" (delete every row) both possible. The cookie (`eagle_session`) is `HttpOnly`,
deliberately not `Secure`, with a ~10-year `max_age`. Credential changes do not invalidate
existing sessions — only "Log out everywhere" does.

---

## 6. Customer Issues & schedule sync architecture (incl. working days, Brief Export)

*(Confirmed unchanged in full — every file this section depends on is absent from the
`867e6a3..b88dd66` diff: `app/services/sync_service.py`, `app/services/customer_issue_
service.py`, `app/services/schedule_service.py`, `app/services/working_days_service.py`,
`app/routers/sync.py`, `app/routers/daily_production.py`, `app/routers/brief.py`, `app/
services/brief_export_service.py`, `scripts/relay_customer_issues.py`. Carried forward
byte-identical from the 09-01 summary on that basis.)*

**Why it looks like this:** a direct fetch from Render to the production brief
(`20.62.194.32:8094`) was tried first and confirmed blocked. All real syncing happens via a
local relay running on a machine that *can* reach the production brief. Three independent
data flows use this same relay/direct-key pattern:

**Customer Issues:** an hourly Windows Task Scheduler job (`scripts/relay_customer_issues.py`)
fetches raw JSON from the brief's `/api/quality-issues` and forwards it unmodified to
`POST /api/v1/sync/customer-issues/ingest-raw`, `X-Relay-Key`-gated (constant-time compare
against `RELAY_API_KEY`). An on-demand "Sync Now" records a pending-request timestamp; a
companion heartbeat records staleness for the UI's connected/disconnected status line.

**Daily schedule:** same relay script also forwards the production brief's scheduled-drawer
counts (scraped from its HTML — no JSON API exists for this) to
`POST /api/v1/sync/daily-schedule/ingest-raw`. **Manual-wins rule:** once a date's row has
`source="manual"`, a later sync write for that date is skipped entirely; a manual write (the
Daily Summary form's own schedule field) always applies immediately.

**Working days** (`app/services/working_days_service.py`) is the single place "is this date a
working day" is decided anywhere in the codebase — never a bare `weekday() < 5` check
elsewhere. A date counts if it has a scheduled or inspected figure > 0, or it's a plain
Mon–Fri weekday not explicitly recorded as a scheduled-zero-and-nothing-inspected holiday. A
missing schedule row (a failed scrape) is never treated as a holiday.

**Brief Export** (`GET /api/v1/brief/summary`, `app/routers/brief.py` +
`app/services/brief_export_service.py`): the mirror of the two flows above — this app pushes
summary data *out* to the production brief's own drawers TV board, read-only, writes nothing.
Gated by a separate `X-Brief-Key` header (constant-time compare against `BRIEF_API_KEY`), an
exact-path exemption from the shared login (the caller is the brief's own VM, no browser
session, calling directly — no relay needed in this direction). Returns
`last_production_day` (last working day's inspected/scheduled/cases/defect-events, all
`null`-never-`0` when genuinely absent) and `week` (week-to-date or prior-full-week summary,
top-3 Pareto categories + an `other_count`, plus a per-working-day breakdown for the brief's
own bar chart) — composed entirely from the same `working_days_service`/`schedule_service`/
`metrics_service` functions the rest of this app already uses, so the TV board's numbers can
never disagree with Reports'.

**Verified locally, NOT verified live:** whether the production brief has actually been
pointed at this endpoint, and whether `BRIEF_API_KEY` is set in Render's dashboard, remain
**NOT VERIFIED** by this sandbox (no access to the brief server or Render's dashboard) —
confirm directly with Rodolfo.

---

## 7. Deployment details

**Environment variables** (source: `app/config.py`, `.env.example`, `render.yaml`):

| Variable | Purpose | Notes |
|---|---|---|
| `DATABASE_URL` | SQLite connection string | Render: `sqlite:////var/data/defect_tracker.db` |
| `UPLOADS_DIR` | Uploaded defect photos | Render: `/var/data/uploads` |
| `APP_HOST` / `APP_PORT` | Local dev-only uvicorn bind | Unused on Render |
| `DISPLAY_TIMEZONE` | Timezone for displayed timestamps | Default `America/New_York` |
| `MAX_UPLOAD_MB` | Photo upload size limit | Default 8 |
| `DEFECT_API_URL` | Where the MCP server reaches the REST API | Not used by the web app itself |
| `PRODUCTION_BRIEF_URL` | Base URL of the Eagle production brief | Real value is a Render secret |
| `SYNC_INTERVAL_MINUTES` | Interval a periodic sync loop would use if re-enabled | Not currently driving anything automatic |
| `DEFAULT_COST_PER_DRAWER` | One-time seed for the `app_settings` `cost_per_drawer` row | No effect after first startup |
| `APP_USERNAME` / `APP_PASSWORD_HASH` | The single shared login credential | Secrets |
| `RELAY_API_KEY` | Shared secret for both Customer Issues and daily-schedule relay ingest | Secret |
| `BRIEF_API_KEY` | Checked against `X-Brief-Key` on `GET /api/v1/brief/summary` | Secret, separate from `RELAY_API_KEY` |
| `RENDER_URL` | The live app's own base URL | Only read by local relay scripts |
| `PYTHON_VERSION` | Pins the Render Python runtime | `3.11.10` |
| `OCR_ENABLED` | Master switch for label-scan OCR | **Default `true`** (changed from `false` — Phase 9 shipped the feature fully; the default engine is free/client-side, so there's nothing to ship dormant) |
| `OCR_PROVIDER` | `"tesseract"` (default, client-side, free), `"azure"`, `"google"`, or `"anthropic"` | Default changed from `azure` to `tesseract` |
| `OCR_ENDPOINT` | Azure Computer Vision resource endpoint | Only read when `OCR_PROVIDER=azure` |
| `OCR_API_KEY` | The OCR cloud provider's API key | Secret; only read for a cloud provider |
| **`FAVORITES_ENABLED`** *(new)* | Kill switch for the favorites quick-pick bars (§2) | Default `false` in `app/config.py`/`.env.example` — **but not declared in `render.yaml` at all**; production has it set directly in Render's dashboard as `true` (confirmed by Rodolfo, "Known production state" above), which works (Render dashboard env vars apply regardless of `render.yaml`) but means a from-scratch Blueprint recreation of this service would silently default it back to `false` unless someone remembers to set it by hand. Worth knowing, not fixed here (documentation task only). |

**`render.yaml`:** one `web` service (`env: python`, `plan: starter`), build `pip install .`,
start `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`, one 1GB disk
at `/var/data`. `OCR_ENABLED`/`OCR_PROVIDER` now declared as `"true"`/`tesseract` (previously
`"false"`/`azure`). `BRIEF_API_KEY`/`OCR_API_KEY`/`OCR_ENDPOINT` remain `sync: false` secrets.
`FAVORITES_ENABLED` is absent, per above.

**Backups:** `scripts/backup_database.py` — unchanged this session (absent from the diff).
Writes to an ephemeral path on Render, not the persistent disk (§8) — this is why this
session's seed-key migration explicitly instructed a manual `cp` to `/var/data/backups/` via
Render Shell *before* running it, confirmed done by Rodolfo (`defect_tracker_20260903_pre_seedkey.db`,
"Known production state" above).

**Live-deployment status — NOT VERIFIED by this sandbox** beyond what's stated under "Known
production state" at the top of this document (no Render API/CLI access, no login
credential): whether the production brief has been pointed at `GET /api/v1/brief/summary`,
and whether a fresh backup exists for anything other than the one pre-seed-key backup
Rodolfo confirmed.

---

## 8. Known limitations / future improvement candidates

- **Test coverage has a documented, structural blind spot: API-level tests prove the API
  works, not that a user can reach it.** This bit twice in one session. (1) `remove_photo`
  and `remove_defect_item` both used to return a deleted-and-committed ORM instance, and the
  router then read attributes off it for the audit log — `sqlalchemy.orm.exc.ObjectDeletedError`,
  a real HTTP 500, discovered only by a live Playwright run against a real running server with
  the app's actual file-backed SQLite database; the in-memory `:memory:` database every
  pytest test uses did **not** reproduce the crash, even with the bug deliberately
  reintroduced to check. A regression test built around this (`tests/api/test_defect_case_
  edit_api.py::test_delete_photo_against_a_real_file_backed_database`) does **not** reproduce
  the original crash either, by design admission in its own docstring — it's kept as ordinary
  additional coverage, not as proof the regression can't recur; only the live server run
  actually caught it. (2) The closed-case item-edit lock shipped with a hint pointing at a
  reopen UI path that did not exist anywhere in the app (§3) — the feature's own API-level
  tests called the status-change endpoint directly and passed, proving the endpoint worked
  without proving any user could reach it. Both incidents are the same shape of gap, not two
  unrelated anecdotes: this is why Playwright was added as an optional dependency this session
  (§1), and why both fixes above were live-verified with it rather than API tests alone.
- **The seed-duplicate incident's fragility is real and current** (§3): roughly 12 inactive
  stations/categories in production are load-bearing — deleting one would recreate the exact
  bug that created it. `customer_issue_categories` carries the identical, unfixed exposure.
- **`docs/PROJECT_SPEC_PHASE9.md` does not exist**, despite being cited by name across a dozen
  files in the Phase 9 codebase (`scan.py`, `ocr_service.py`, `label-scan.js`, `schemas.py`,
  `models.py`, the line_label migration, `defect_entry.html`) as the authoritative spec. No
  commit in this repo's history ever added it. This document is built entirely from the code,
  schemas, migration, and tests actually on disk, not from that missing document's claims.
- **`docs/DATA_DICTIONARY.md` has not been updated** for `line_label`/`entry_source`/
  `is_favorite`/`favorite_rank`/`seed_key` — confirmed by grepping the file for those names
  (zero matches) and by its absence from the `867e6a3..b88dd66` diff.
- **`FAVORITES_ENABLED` is not declared in `render.yaml`** (§7) — works today via a
  dashboard-set env var, but a from-scratch Blueprint recreation of the service would silently
  default it back off.
- **The OCR line-label read's confidence floor and distance-cap constants are explicitly
  documented in the code as "not calibrated against a real label"** beyond the handful of real
  photos used to build the five same-day hotfixes; there is no JS test framework in this repo,
  so every hotfix's client-side geometry logic was verified via disposable, uncommitted
  Node.js simulation harnesses rather than a repeatable automated suite.
- **Favorites' manual reordering (drag/up-down) was deliberately scoped out** as a fast-follow,
  not built — `favorite_rank` is server-assigned only.
- **A more durable fix for the seed-duplicate bug (backfilling `seed_key` onto the renamed
  rows instead of relying on the dead duplicates) was discussed and deliberately deferred**,
  pending a human-confirmed rename mapping — not safely inferable from the data alone, though
  likely reconstructable from `audit_log`'s existing before/after records for any rename done
  through the app's own UI.
- **The root network restriction between Render and the production brief is still
  unresolved** — the local relay is a working workaround, not a fix.
- **Uploaded photos are not covered by any backup process.**
- **No per-user accounts** — by design (single shared login).
- **`scripts/backup_database.py` writes to an ephemeral path on Render, not the persistent
  disk.**
- **The scheduled-drawer-count sync depends on scraping the production brief's HTML** — a
  fragile dependency.
- **`daily_schedules` has no shift dimension by design.**
- **A defect case created before the `cost_per_drawer_at_time` column existed has no cost
  snapshot** — falls back to the currently-configured admin rate.

---

## 9. Testing & tooling

- **676 tests pass**, verified by actually running
  `./.venv/Scripts/python.exe -m pytest -q` this session (up from 534 at the time of the
  09-01 summary): **391** in `tests/unit/`, **269** in `tests/api/`, **16** in `tests/mcp/`.
  New test files since 09-01: `tests/api/test_master_data_api.py`,
  `tests/api/test_defect_case_edit_api.py`, `tests/api/test_favorites_api.py`,
  `tests/unit/test_seed_data.py` (this session's work), plus Phase 9's
  `tests/api/test_scan_api.py`, `tests/unit/test_ocr_service.py`,
  `tests/unit/test_line_label.py`, `tests/unit/test_phase9_migration.py`,
  `tests/unit/test_scan_vendor_assets.py`.
- **Linting:** Ruff 0.8.4 (`pyproject.toml`, unchanged). `ruff check .` passes clean;
  `ruff format --check .` passes clean with **no exceptions** — the 09-01 summary's one
  standing failure (`app/services/ocr_service.py`, then untracked) is gone now that the file
  is committed and formatted.
- **Test isolation:** unchanged — an in-memory SQLite database per test (`StaticPool`),
  FastAPI dependency overrides for `get_db`, a pre-authenticated session cookie on the shared
  `client` fixture. One exception this session: a dedicated regression test
  (`test_delete_photo_against_a_real_file_backed_database`) builds its own file-backed SQLite
  engine specifically to get closer to production's real database shape — though even that
  did not reproduce the live-server bug it was written around (§8).
- **Live-browser verification (new this session):** Playwright 1.62.0, declared as an
  optional `e2e` dependency (§1). Used to catch and confirm the fix for the `ObjectDeletedError`
  photo-delete crash, to verify the Favorites feature end-to-end (Admin favoriting → New
  Defect quick-pick bars → the 5-cap → the kill switch's fallback, in both directions,
  against a real server), and to verify the reopen-control fix (create → close → reopen →
  edit → re-close, through the actual browser). Not part of the pytest suite; run manually
  against an isolated scratch database/uploads directory/port, never the real
  `data/defect_tracker.db`/`uploads/`, confirmed untouched after every run this session.

---

## 10. File/module structure

```
app/
  main.py                  FastAPI app instance, lifespan/startup, page routes, static/uploads mounts
                            - now imports and registers app/routers/scan.py (Phase 9, re-added correctly)
  config.py                Settings (env vars), cached via @lru_cache
                            - ocr_enabled now defaults True, ocr_provider defaults "tesseract" (Phase 9)
                            - gained favorites_enabled, default False (this session)
  database.py              SQLAlchemy engine/session setup, SQLite pragmas (unchanged)
  models.py                All ORM models (persistence only) - 13 tables
                            - Station/DefectCategory gained is_favorite/favorite_rank/seed_key
                            - DefectCase gained line_label/entry_source + a composite index
  schemas.py               Pydantic request/response models
                            - Scan*/DefectCase line_label/entry_source (Phase 9)
                            - StationOut/DefectCategoryOut gained is_favorite/favorite_rank/
                              created_at_local; MasterDataOut gained favorites_enabled;
                              new DefectItemUpdate
  seed_data.py             Baseline master data + credential sync, run on every startup
                            - _seed_missing() now matches by name OR seed_key (this session)
  auth_middleware.py       LoginRequiredMiddleware - gates every route except a small allowlist (unchanged)
  dependencies.py           Shared FastAPI dependencies (unchanged)
  errors.py                 Shared ServiceError hierarchy -> uniform JSON error envelope (unchanged)
  timezone_utils.py         Display-timezone conversion + calendar-only date-preset resolution (unchanged)
  routers/
    auth.py                  Login / logout / logout-everywhere (unchanged)
    brief.py                 GET /api/v1/brief/summary, X-Brief-Key-gated (unchanged)
    defect_cases.py          Create/list/edit/status/delete/photos for DefectCase
                              - gained item add/edit/remove + photo delete endpoints (this session)
                              - line_label filter on list_cases (Phase 9)
    daily_production.py     Daily Production Summary CRUD + schedule CRUD/attainment (unchanged)
    reports.py               KPI summary, Pareto, trend, work-order history, rework queue, date-preset
                              - line_label filter param; by_line work-order-history breakdown (Phase 9)
    customer_issues.py       Customer Issues CRUD + CSV export (unchanged)
    sync.py                  Production-brief sync control + relay ingest/heartbeat (unchanged)
    master_data.py           Stations/categories/priorities/statuses/dispositions
                              - active_only param, favorites_enabled field, delegates field
                                edits + favorites to master_data_service.py (this session)
    exports.py               Defect CSV export - gained a line_label filter param (Phase 9)
    settings.py              Admin-editable app settings (cost_per_drawer) (unchanged)
    scan.py                  GET /config, POST /parse-label, POST /diagnose (Phase 9) - now
                              imported/registered in main.py; fully part of the running app
  services/
    defect_service.py        Case numbering, creation, status transitions, cost snapshot
                              - normalize_line_label() (Phase 9); item/photo edit + reopen-gate
                                logic, apply_case_field_updates() (this session)
    metrics_service.py        KPI/Pareto/trend/sort/cost/schedule-attainment math
                              - filtered_defect_items_query() gained a line_label param (Phase 9)
    master_data_service.py    NEW (this session) - station/category field edits + favorites max-5
    schedule_service.py       Daily schedule CRUD + manual-wins sync upsert (unchanged)
    working_days_service.py   The single source of truth for "is this date a working day" (unchanged)
    brief_export_service.py   Business logic behind GET /api/v1/brief/summary (unchanged)
    customer_issue_service.py Customer Issue business rules (unchanged)
    sync_service.py           Customer Issues sync + relay heartbeat/pending-request (unchanged)
    auth_service.py           Login/session/credential-sync logic (unchanged)
    settings_service.py       Generic app_settings read/write helpers (unchanged)
    audit_service.py          Writes AuditLog rows (unchanged - new callers only, no signature change)
    export_service.py         CSV export generation - gained a line_label column (Phase 9)
    ocr_service.py            Label-scan OCR parsing/geometry/validation pipeline (Phase 9) -
                              now committed and imported by app/routers/scan.py; part of the running app
  templates/                 Server-rendered Jinja2 pages
                              - defect_entry.html: Scan label button/modal, line label picker,
                                favorites bars for Found Station/Possible Source/categories
                              - admin.html: Favorite checkbox + count, Created column
                              - rework_queue.html: stale reopen-related comment corrected
  static/                    Plain CSS/JS, no build step
                              - js/app.js: case-detail modal gained station/item/photo edit
                                controls, the Reopen control, favorites-bar rendering
                              - js/api.js: gained item/photo-delete/favorites API calls
                              - js/label-scan.js: NEW (Phase 9) - client-side scan pipeline
                              - js/vendor/jsqr.js, tesseract.min.js, tesseract-worker.min.js,
                                tesseract-core-lstm.wasm.js, eng.traineddata.gz: NEW (Phase 9),
                                vendored verbatim, no CDN dependency
alembic/versions/            Twelve migrations (three more than 09-01's nine), in order:
                              initial schema -> customer-issue tables + source_thread_id/sync_logs ->
                              resolved_on_the_spot/skipped_recheck -> app_settings/cost_per_drawer ->
                              auth_sessions -> daily_schedules -> cost_per_drawer_at_time on
                              defect_cases -> migrate open legacy status/disposition ->
                              line_label/entry_source on defect_cases (Phase 9) ->
                              is_favorite/favorite_rank on stations/defect_categories (this session) ->
                              seed_key on stations/defect_categories (head, this session)
mcp_server/server.py         Stdio MCP server - calls the REST API, never touches SQLite directly (unchanged)
scripts/
  relay_customer_issues.py    Hourly full relay (unchanged)
  relay_poll.py                Frequent heartbeat/on-demand-sync poller (unchanged, not scheduled)
  backup_database.py           SQLite backup utility (unchanged)
  seed_demo_data.py / seed_customer_issues.py  Synthetic data for local development (unchanged)
tests/                       unit/ (391), api/ (269), mcp/ (16) - see §9
docs/                        PROJECT_SPEC.md + phase addenda (no PHASE9 addendum exists - see §8),
                              DATA_DICTIONARY.md (stale re: this session's + Phase 9's new
                              columns - see §8), PRODUCTION_BRIEF_SCHEDULE_SOURCE.md, setup/user
                              guides, WEEKEND_SCHEDULE_CLEANUP_RUNBOOK.md - none of these
                              changed since the 09-01 summary
render.yaml                  Render Blueprint - OCR_ENABLED/OCR_PROVIDER defaults changed;
                              FAVORITES_ENABLED absent (see §7)
.env.example                 Documented template - OCR defaults changed; gained FAVORITES_ENABLED
pyproject.toml                Dependencies, Ruff config, pytest config - gained an optional
                              `e2e` dependency group (playwright==1.62.0)
CLAUDE.md                     Gained an explicit warning about load-bearing inactive
                              stations/categories (this session's seed-duplicate finding)

(repo root, untracked, not part of the app - unchanged since 09-01, not re-verified in detail)
EAGLE_PRODUCTION_BRIEF_TECHNICAL_KNOWLEDGE.md
EAGLE_PRODUCTION_BRIEF_TECHNICAL_KNOWLEDGE_2026-09-01.md
before_drawers_2026-08-31.html
```

---

## 11. Date presets

*(Unchanged — `app/timezone_utils.py`, `app/services/working_days_service.py`, and `app/
templates/_date_presets.html` are all absent from the `867e6a3..b88dd66` diff. Carried
forward byte-identical from the 09-01 summary.)*

`GET /api/v1/reports/date-preset?preset=<name>` resolves one of seven preset buttons to a
concrete `{start_date, end_date}`, in `DISPLAY_TIMEZONE`, dispatching between two independent
implementations:

| Preset | Resolved by | Definition |
|---|---|---|
| `today` | `timezone_utils.resolve_date_preset` (pure, no DB) | The current date in `DISPLAY_TIMEZONE`. |
| `this_week` | same | Monday of the current week through today — week-to-date. |
| `last_week` | same | The calendar Monday–Friday immediately before the current week. |
| `month_to_date` | same | The 1st of the current month through today. |
| `yesterday` | `working_days_service.resolve_working_day_preset` (DB-backed) | The previous working day. |
| `last_7_days` | same | The trailing 7 working days through today. |
| `last_30_days` | same | Same, 30 working days. |

Both Dashboard and Reports share one preset-button partial (`_date_presets.html`) and one JS
wiring function (`initDatePresetButtons`), and both resolve their load-time default range from
the server (`last_7_days`/`last_30_days` respectively) rather than computing it independently
in JavaScript — so opening a page and clicking its own seemingly-matching preset button can
never produce a different range than what was already showing.
