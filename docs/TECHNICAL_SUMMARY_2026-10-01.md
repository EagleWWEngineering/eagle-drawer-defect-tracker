# Eagle Drawer Defect Tracker — Technical Summary (2026-10-01)

**This covers the changes since [`TECHNICAL_SUMMARY_2026-09-03.md`](TECHNICAL_SUMMARY_2026-09-03.md).**
The 09-03 document is the full snapshot and is left untouched. Anything not mentioned here is
unchanged from it: architecture, the auth system, KPI formulas (apart from the FPY floor below),
working days, Customer Issues sync, and Brief Export. If this file and the code disagree, trust
`git log` and the code.

**Verified at:** `a23e483` (`master`, live on Render). The range `b88dd66..a23e483` is 8 commits.

- **Tests:** `pytest -q` → **719 passed**.
- **Lint:** `ruff check` is clean.
- **Migrations:** the Alembic head is `c9f4a1e6d3b2`, with 15 migration files.
- **Production:** the live endpoints were checked on Render (401 without the key; 200 with the
  real key and an empty batch). Rodolfo confirmed on the floor that label scans, photos and UNDO
  kickbacks work.

| Commit | What |
|---|---|
| `40c8b9c` | Phase 9 finish: OCR removed entirely; QR-only scan plus a permanent A–Z line picker |
| `4aa9982` | Phase 10: production-count feeds (completed drawers, order lines, label resolve) |
| `55d6f10` | Completed feed ignores dates before 2026-09-30, so typed history is kept |
| `3963cad` | New Defect: "Save with photo" in one tap; equal-size Rework / Set Aside buttons |
| `162a372` | Session log: one-tap camera upload; no separate Attach button |
| `a23e483` | Phase 11: UNDO-card kickbacks open Set Aside cases; a later scan closes them |

---

## 1. Where data comes from now

The tracker stays on Render, which can't reach eagle-vm. **`eagle-drawers-production-count`
("production count") on eagle-vm pushes everything** to relay-key endpoints. Every one of them:

- uses the `X-Relay-Key: <RELAY_API_KEY>` header;
- is exempt from the login middleware for its exact path only (`app/auth_middleware.py`
  `PUBLIC_EXACT_PATHS`);
- checks the key before reading the body;
- rejects a malformed body as a whole with `422`, writing nothing.

The laptop relay (`EagleDefectTracker-RelaySync`) has been **disabled since 09-30**.

| Endpoint | Sent | Service |
|---|---|---|
| `customer-issues/ingest-raw` | hourly :30 + Sync Now heartbeat | `sync_service` (unchanged) |
| `daily-schedule/ingest-raw` | hourly :30 + Sync Now | `schedule_service` (unchanged) |
| `daily-completed/ingest-raw` | 06:00 + 15:30 ET, trailing 7 days | `daily_completed_service` |
| `order-lines/ingest-raw` | hourly :30, open-orders snapshot | `order_line_service` |
| `drawer-events/ingest-raw` | every 60 s | `drawer_event_service` |

**Secrets:** `RELAY_API_KEY` is in `.env` and in Render's environment, and Render's value is the
authoritative one. The `BRIEF_API_KEY` in `.env` is **stale**: Render returns 401 for it. Never
give `BRIEF_API_KEY` to production count.

## 2. Phase 10 — completed drawers and unique-ID labels (`docs/PROJECT_SPEC_PHASE10.md`)

**Completed drawers**
- `DailyProductionSummary.drawers_inspected` is written **only** by the daily-completed feed: one
  unique, valid QC scan per drawer, per shop-local day.
  - The Daily Summary form shows it read-only.
  - `DailyProductionSummaryIn` rejects it with a 422.
  - The MCP `record_daily_production` no longer takes it.
- The feed upserts the `(date, "Day")` row. A count of 0 with no existing row writes nothing.
- `FEED_START_DATE = 2026-09-30`: earlier dates are validated but never written, so hand-typed
  history is kept.
- `ck_rejected_le_inspected` was dropped (migration `a7d2e9c4b1f0`). Rejected and completed are
  now independent, and **First Pass Yield floors at 0%**.

**Unique-ID labels**
- The label QR is `…WorkOrderPDFs/\179459.pdf#drawer=285016-1`. The fragment holds the
  `order_detail_id` and the unit.
- `app/services/label_service.py` is the **only** parser (`POST /api/v1/labels/resolve`, login
  required). JS just calls it.
- An `order_detail_id` that's known (and on the same order) fills the line letter and shows the
  drawer details. An unknown one fills only the order number.
- `order_lines` (migration `b8e3f0d5c2a1`) is keyed by `order_detail_id`. Lines are replaced per
  order, and an order missing from a snapshot is never deleted.
- `defect_cases` gained `order_detail_id` (indexed) and `drawer_unit`. `drawer_unit` requires
  `order_detail_id`.

## 3. New Defect photo flow (`3963cad`, `162a372`)

- **"Save with photo"** validates first, then opens the rear camera **synchronously inside the
  tap** (phones block it otherwise). Taking the picture saves the case and then uploads it.
  "Save without photo" still exists.
- Every upload is shrunk in the browser first: JPEG, at most 1600 px, quality 0.82
  (`api.js` `shrinkPhotoForUpload`).
- The session log's photo cell is one button: "📷 Add photo", which becomes "📷 attached" /
  "📷 N photos" plus "+ Add another". Uploading starts when the picture is taken.
- Rework / Set Aside are a single 2-column grid of equal buttons (`.disposition-grid`).
- These are pinned by `tests/unit/test_save_with_photo.py`, since the repo has no JS test runner.

## 4. Phase 11 — UNDO-card kickbacks and auto-close (`docs/PROJECT_SPEC_PHASE11.md`)

Rodolfo's decisions:
- The existing **UNDO card** is the kickback signal, with no new QR. It's never used for mistakes.
- Rework cases resolved on the spot never reach the queue, so they aren't involved.

Production count sends `{event_id, type, area, order_no, order_detail_id, unit, occurred_at}`
events. `app/services/drawer_event_service.py` applies them in `(occurred_at, event_id)` order.

**Kickback** (the UNDO card voided the drawer's count)
- If the drawer has no open case, a case is opened as Set Aside, Open, Normal, quantity 1, with
  `entry_source="undo_card"`.
- Line letter: from `order_lines`, same-order only.
- Found station: **Assembly** or **QC / Sorting / Shipping**, looked up by `seed_key`.
- Note: "Kicked back with the UNDO card at QC, 10:42 AM. Problem not described yet."

**Counted** (an accepted +1 at any station)
- Every open case for that drawer with `detected_at` before the scan closes as **Closed –
  Repaired**. The history note is "Auto-closed: scanned at QC 10:42 AM".
- This covers phone-logged cases with a drawer id too.
- A +1 from before the kickback never closes it.

**Rules**
- **Matching:** only by `(order_detail_id, drawer_unit)`. Only new labels name one drawer.
- **Resends:** `drawer_events` (migration `c9f4a1e6d3b2`) stores every event, unique on
  `(source, event_id)`, with an `outcome` string. A resend is a no-op, so it can't reopen a
  closed drawer.
- **Failed events:** an event that can't be applied is stored as `failed: …`, so it isn't
  retried forever.
- **Logging:**
  - `sync_logs` gets a row only when a push created or closed a case, or failed; plain +1 pushes
    would bury the Admin Sync Log.
  - The audit log uses `actor_role="production-count"`.
- **Category:** Admin → **UNDO Card Kickbacks** sets it, per area (`GET/PUT
  /api/v1/settings/undo-categories`).
  - The app settings `undo_category_qc` / `undo_category_assembly` store the category **id**.
  - If unset, it falls back to Other (by `seed_key`).
  - Rodolfo created and selected "QC Kick Back" and "Assembly Kick Back" on 10-01.
  - **Code never creates, renames or seeds categories.**
- **Rework Queue:** shows the line letter, `Drawer <id>-<unit>`, and an **UNDO card** badge.
  `ReworkQueueItemOut` gained `line_label`, `entry_source`, `order_detail_id` and `drawer_unit`.
- **Latency:** usually under a minute from scan to case. The queue page doesn't auto-refresh.

## 5. Known limitations / candidates

- Typed or old-style cases (no drawer id) never auto-close; they're closed by hand.
- The Rework Queue doesn't auto-refresh.
- Photo access for production count, browse versus QC-station display, was discussed but not
  decided and not built.
- The order-only label share from the Access export (the 09-23 investigation) limits how many
  scans carry a drawer id. Production count reports skipped scans on its admin page.
