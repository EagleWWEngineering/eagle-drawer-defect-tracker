# Phase 10 Addendum — Production Count Feeds (Completed Drawers + Unique-ID Labels)

Addendum to [`PROJECT_SPEC.md`](PROJECT_SPEC.md) and the Phase 2–9 addenda.
Everything in them still applies except where this document says otherwise.
Decisions by Rodolfo, 2026-09-30.

`eagle-drawers-production-count` ("production count") runs on eagle-vm next to
the production brief and holds every QC/Sorting scan. This app (on Render) can't
reach eagle-vm, so production count **pushes** to two new relay-key ingest
endpoints. It is also taking over the two existing relay feeds (customer issues,
daily schedule) from Rodolfo's laptop, using the existing ingest endpoints
unchanged — no change in this app for those; `scripts/relay_*.py` stay in the
repo, the laptop scheduled task is simply disabled.

Both new endpoints use the same `X-Relay-Key: <RELAY_API_KEY>` check as
`/customer-issues/ingest-raw` (no new secret), are exempt from the login
middleware (exact paths only), write one `sync_logs` row per successful call,
and reject an invalid body **as a whole** with `422` and nothing written. A
missing/wrong key is `401` and is checked before the body is looked at.

---

## Part 1 — Drawers completed feed

### Decisions

1. **Automatic feed only.** `DailyProductionSummary.drawers_inspected` is written
   only by this feed: production count's "Completed (QC/Sorting)" — unique,
   valid QC scans per day (one per drawer that finished the whole line; repeats,
   order-only labels and undone scans excluded).
   - Daily Summary form: shown read-only as "Drawers completed (from Production
     Count)", never sent on save. "—" until the feed has sent that date.
   - `PUT /api/v1/daily-production/{date}` rejects a body containing
     `drawers_inspected` (`422`). `defect_service.upsert_daily_summary` has no
     `drawers_inspected` parameter; an existing row keeps its fed value, a new
     manually-saved row starts at 0 until the next feed run.
   - The MCP `record_daily_production` tool no longer takes `drawers_inspected`.
   - `drawers_rejected_unique`, reworked, scrapped, notes and the cost snapshot
     stay manual exactly as before.
2. **One shift:** the feed always writes shift `"Day"`. Other-shift rows (legacy
   only) are never touched, and are still summed per date as before.
3. **Completed and rejected are independent KPIs.** `ck_rejected_le_inspected`
   is dropped (migration `a7d2e9c4b1f0`, SQLite batch table rebuild — every
   other constraint and index kept), and the matching hard rule in
   `validate_daily_summary_input` is gone. Rates when rejected > inspected:
   rejection rate / defects per 100 / rework rate are reported as computed (can
   exceed 100% — an honest sign the two sources disagree for that range); **First
   Pass Yield is floored at 0%** (`metrics_service.compute_kpis` and the form's
   preview). `drawers_inspected == 0` is still `null`/"N/A" everywhere.
4. **Timing** (production count's side): sends at 06:00 (finalizes yesterday)
   and 15:30 (today at shift end) America/New_York, each time re-sending the
   trailing 7 days so corrections (a QC undo, a late import) flow through.

### Contract — `POST /api/v1/sync/daily-completed/ingest-raw`

```json
{
  "source": "eagle-drawers-production-count",
  "station": "QC_SORTING",
  "generated_at": "2026-09-30T19:30:00Z",
  "counts": {"2026-09-24": 0, "2026-09-25": 171, "2026-09-30": 57}
}
```

Keys are shop-local (America/New_York) calendar dates — the same day boundary as
`production_date`. Values are integers ≥ 0 (a JSON bool, float, string or null
is rejected).

Per date:

| State | Action |
|---|---|
| `(date, "Day")` row exists | set `drawers_inspected = count` (0 included); every other column untouched |
| no row, count > 0 | create it: `drawers_inspected = count`, other counts 0, `notes` null, `cost_per_drawer_at_time` snapshotted like a form-created row |
| no row, count == 0 | nothing — no empty weekend/holiday rows (the brief reads "row exists" as "entered") |

A weekend date with count > 0 is recorded, not rejected (real overtime scans;
`working_days_service` already treats inspected > 0 as a working day).

Response: `{"received": N, "updated": N, "created": N, "skipped_zero": N}`.
Idempotent: the same body twice leaves identical state (the second call reports
its rows as `updated`). `sync_logs.source_url` is
`relay:eagle-drawers-production-count/daily-completed/QC_SORTING`.

The production brief reads the fed value through the existing
`GET /api/v1/brief/summary` with no brief-side change.

**Existing data:** hand-typed `drawers_inspected` values stay. The first feed run
overwrites the trailing 7 days; older days keep their typed history.

Code: `app/services/daily_completed_service.py`, `app/routers/sync.py`.

---

## Part 2 — Unique-ID drawer labels

Drawer labels now carry a per-drawer fragment:

```
https://eagledovetaildrawers.sharepoint.com/:b:/r/sites/Server/Documents/AccessDB/WorkOrderPDFs/\179459.pdf#drawer=285016-1
```

`179459` = order, `285016` = Access order-line record id (`order_detail_id`),
`1` = unit within that line. Old labels have no fragment ("order-only"). The
backslash before the order number is malformed at source and one-or-more
slashes/backslashes are tolerated.

### Contract — `POST /api/v1/sync/order-lines/ingest-raw`

Full snapshot of every open order production count has line detail for, hourly:

```json
{
  "source": "eagle-drawers-production-count",
  "generated_at": "2026-09-30T19:00:00Z",
  "orders": {
    "179459": {"lines": [
      {"order_detail_id": 285016, "line": "A", "qty": 2,
       "detail": {"Size": "8 x 37.875 x 27", "Wood": "maple", "Bottom": "1/4",
                  "Options": "N&B 2", "Notes": ""}}
    ]}
  }
}
```

- Each order in the body has its stored lines **replaced** (a line no longer
  listed for that order is removed).
- Orders **absent** from the body are kept forever — a closed/shipped order drops
  out of the open-orders snapshot, but a defect can be logged after shipping.
- Validation (422, whole request): order keys must be digits; `lines` a list;
  `order_detail_id` a positive int, unique across the body; `line` a non-empty
  string (normalised upper-case); `qty` int ≥ 0 or null; `detail` object or null.
- Response: `{"orders": N, "lines": N}` (counts in this snapshot).

Stored in `order_lines` (migration `b8e3f0d5c2a1`), keyed by `order_detail_id`:
`order_no`, `line`, `qty`, `detail_json`, `received_at`.

### Label behavior

- Parsed in **one** place, server-side: `app/services/label_service.py`,
  exposed as `POST /api/v1/labels/resolve` `{"text": "<raw QR text>"}` (behind
  the normal login). `app/static/js/label-scan.js` now only decodes the QR and
  hands back the raw text.
- Returns `{order_no, order_detail_id, unit, line_known, line_label, qty, detail}`.
- **Known** `order_detail_id` (pushed, and pushed under the same order): fills the
  order number **and** line letter, and shows the drawer's details (line, "drawer
  1 of 2", size, wood, bottom, options) under the line picker so the operator can
  confirm it's the right drawer. `entry_source = "scanned"`, or
  `"scanned_edited"` if the line is then changed by hand.
- **Unknown** `order_detail_id` (not pushed yet, or pushed under a different order
  — never trusted across orders): fills the order number; the A–Z picker is used
  for the line exactly as today. The drawer identity is still kept.
- **Old order-only label:** exactly as today.
- Drawer identity is stored on the case: `defect_cases.order_detail_id` +
  `drawer_unit` (nullable; null for manual and order-only entries), alongside
  `work_order_number`. Typing a different order over a scanned one drops the
  identity; `drawer_unit` without `order_detail_id` is rejected (400). Shown in
  the case-detail modal as "Drawer: 285016-1".

Code: `app/services/order_line_service.py`, `app/services/label_service.py`,
`app/routers/labels.py`, `app/templates/defect_entry.html`.
