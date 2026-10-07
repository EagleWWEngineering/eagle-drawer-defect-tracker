# Phase 12: the 2026-10 redesign and the move to eagle-vm

Decided with Rodolfo on 2026-10-06. Built on branch `redesign-2026-10` (off
`vm-migration`), deployed to eagle-vm in one go after his review.

## 1. Hosting (live since 2026-10-06 14:11 ET)

- Off Render, onto eagle-vm: user service `eagle-drawer-defect-tracker.service`,
  127.0.0.1:8112. Shop access through production count at `:8105/defects/`
  (no new firewall rule). `ROOT_PATH=/defects`, `LOGIN_REQUIRED=false`.
- Nightly snapshot of the database and photos (02:20) into `~/state-backups/local`,
  picked up by the VM's 03:18 off-site export (Azure + Backblaze). Photo tarballs
  kept 2 nights on the VM.
- `GET /api/v1/sync/export/{database,uploads}` (relay key) existed only to copy the
  data off Render. Remove once Render is deleted.

## 2. Look

One visual language with production count: Barlow / Barlow Condensed
(self-hosted), steel-blue accent, orange only for kickbacks, floor cards for
drawers, tiles for numbers. Print Log removed.

## 3. Pages

- **Rework & Kickback Queue** (was Rework Queue; same URL): one card per drawer
  (WO, line, customer large; size/wood/bottom/options; defect; found station;
  clamp; Kickback/QC tag; priority stripe; age). Search (every word must match; a
  scanned label jumps to the drawer), "Include closed (last 7 days)", 30 s
  auto-refresh that pauses while someone is working on a card.
- **New Defect**: same field order. No letter picker, no typed line, no possible
  source. Scanned drawer card. Duplicate warning: "Add to that case" (keeps the
  existing categories, appends notes, priority only goes up, fixed-on-the-spot
  closes it) or "Log a new defect anyway".
- **Quality Today** (Dashboard): today, this week vs the same days last week,
  20-working-day trend with the Admin target, top 5 defects, kickbacks per day and
  by clamp, oldest open drawers, customer issues this week, feed health. `?tv=1`.
- **Reports**: production count's date chips; one filter panel drives everything
  (including the trend); Type filter (kickback/QC); Pareto by category / found at /
  line / clamp; how long cases stay open; caught here vs reached the customer;
  records and CSV with customer and size.
- **Admin**: Delete/Restore stations and categories (soft); Dashboard target; feed
  health table.
- **Daily Summary**: unique drawers rejected is automatic (see 4).

## 4. Data changes (migrations after c9f4a1e6d3b2)

| Revision | Change |
|---|---|
| d1a7c3e9f2b4 | `work_orders` (customer name per order, from production count's order-lines feed) |
| e2b8d4f0a3c5 | `is_deleted` / `deleted_at` on stations and defect_categories |
| f3c9e5a1b4d6 | `daily_production_summaries.rejected_source` ('auto' / 'manual') |
| a4d0f6b2c8e1 | `feed_statuses` (feed health) |
| b5e1a7c3d9f2 | `drawer_events.clamp` |

**Automatic rejected count (spec addendum to PROJECT_SPEC.md 2.1).** The formulas
are unchanged; only the source of "unique drawers rejected" changes. 'auto' rows use
the count of distinct non-deleted cases on that production date, kickbacks
included (on the Day row only). A number saved on Daily Summary is 'manual' and
wins; "Use automatic" switches back. Existing rows from 2026-09-30 on that still
held 0 became 'auto'.

## 5. Production count changes (eagle-drawers-production-count)

- `/defects/*` pass-through and a Defects menu item.
- Order-lines feed carries `customer` per order.
- Drawer events carry `clamp`: the clamp that assembled the drawer (its last
  accepted Assembly In scan up to the event).

## 6. Not done (optional, later)

- Kickbacks that never reach the tracker: the on-screen Undo and admin voids
  (FAILED_INSPECTION / DAMAGED) don't write an UNDONE row.
- Slack alert when a feed stays late (production count already posts to Slack);
  the channel is undecided.
