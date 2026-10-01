# Phase 11 Addendum — UNDO-Card Kickbacks and Auto-Close on Re-Scan

Addendum to [`PROJECT_SPEC.md`](PROJECT_SPEC.md) and the Phase 2–10 addenda.
Everything in them still applies except where this document says otherwise.
Decisions by Rodolfo, 2026-10-01.

## Why

QC and Assembly In operators can't stop to fill in the New Defect form. When a
drawer is kicked back for rework they already scan the **UNDO card** and then the
drawer, which takes it off that station's count in
`eagle-drawers-production-count` ("production count"). And once a drawer comes
back from rework and is scanned again, the rework is done, but its case used to
sit Open in the Rework Queue until someone closed it by hand.

## Decisions

1. **The existing UNDO card is the kickback signal. No new QR card.** The UNDO
   card is only ever used to kick a drawer back (never for a mistaken scan, since
   every drawer has a unique ID), so every UNDO opens a case.
2. **A kickback opens a Set Aside case** with:
   - **Disposition:** Set Aside. **Status:** Open. **Priority:** Normal.
   - **Quantity:** 1.
   - **Work order:** from the label.
   - **Line letter:** from `order_lines`, only when that line belongs to the same
     order. Otherwise it is left blank.
   - **Drawer:** `order_detail_id` and `drawer_unit`.
   - **`detected_at`:** the scan time. **`production_date`:** the scan's
     shop-local date.
   - **`entry_source`:** `undo_card`.
   - **Note:** "Kicked back with the UNDO card at QC, 10:42 AM. Problem not
     described yet."
   - **Found station:** Assembly In maps to the **Assembly** station and QC to
     **QC / Sorting / Shipping**. Both are found by `seed_key`, so renaming them in
     Admin doesn't break the mapping.
   - **Category:** chosen per area in **Admin → UNDO Card Kickbacks**. Rodolfo is
     creating "QC Kick Back" and "Assembly Kick Back" himself. If none is chosen,
     the built-in **Other** category is used (found by `seed_key`). The setting
     stores the category id, so renames are safe. **Code never creates, renames or
     seeds categories for this.**
   - **Already-open drawer:** if the drawer already has an open case, no second
     case is made.
3. **Any counted (+1) scan of the drawer at any station closes it.** Every open
   case for that drawer with `detected_at` earlier than the scan becomes
   **Closed – Repaired**, with the note "Auto-closed: scanned at QC 10:42 AM".
   - This covers cases logged on a phone from a scanned label too.
   - A +1 from *before* the kickback (QC scans first, then UNDOes it) never closes
     the case, even if that +1 arrives late.
4. **Closing by hand is unchanged**, and the auto-close never touches an
   already-closed case.
5. **New-style labels only.** Matching is always by `(order_detail_id, unit)`,
   because only a unique-ID label names one drawer. An order-only scan creates and
   closes nothing.
6. Rework cases resolved on the spot are closed at entry, so they never reach the
   queue and this feature doesn't involve them.

## Endpoint

`POST /api/v1/sync/drawer-events/ingest-raw` uses the same
`X-Relay-Key: <RELAY_API_KEY>` check as the Phase 10 feeds. It is exempt from the
login middleware for this exact path only, and a missing or wrong key returns
`401` before the body is read.

```json
{"source": "eagle-drawers-production-count",
 "events": [{"event_id": 9123, "type": "kickback", "area": "qc",
             "order_no": "179459", "order_detail_id": 285016, "unit": 1,
             "occurred_at": "2026-10-01T14:42:07+00:00"}]}
```

| Field | Rule |
|---|---|
| `event_id` | positive int: production count's own scan id |
| `type` | `kickback` (UNDO card voided the drawer's count) or `counted` (an accepted +1) |
| `area` | `qc` or `assembly` |
| `order_no` | 1–20 letters or digits |
| `order_detail_id`, `unit` | positive ints |
| `occurred_at` | ISO datetime **with** a timezone |

Response: `{received, duplicates, cases_created, cases_closed, failed}`. This is a
contract with production count, so the keys must not be renamed.

- **Validation:** any malformed event returns `422` for the whole request, and
  nothing is written. At most 1000 events per request.
- **Order:** events are applied in `(occurred_at, event_id)` order.
- **Storage:** each event is stored in `drawer_events`, which is unique on
  `(source, event_id)`. A resend counts as a duplicate and changes nothing, so a
  re-sent kickback can't reopen a drawer that has since been closed.
- **Events that can't be applied:** if an event can't be acted on (for example,
  the station is missing), it is still stored, with `failed: …` as its outcome, so
  it isn't retried forever.
- **Sync log:** a `sync_logs` row is written only when a push created or closed a
  case, or had a failure. Most pushes are plain +1 scans that touch nothing, and
  logging them would bury the Admin Sync Log.
- **Audit log:** cases created or closed this way are recorded with
  `actor_role = "production-count"`.

## Settings

`GET/PUT /api/v1/settings/undo-categories` takes
`{qc_category_id, assembly_category_id}`, both nullable, where null means Other.
It requires a login, returns `400` for an unknown category id, and is audited.

## UI

- **Admin → UNDO Card Kickbacks:** two dropdowns. They list active categories,
  plus an inactive one if it is the current choice.
- **Rework Queue:** shows the line letter, `Drawer <id>-<unit>`, and an
  **UNDO card** badge on cases opened this way.

## Production count side

Production count pushes both event types as they happen. It retries from its last
pushed id when Render is unreachable.

- `counted` = every accepted, non-voided scan event with a drawer identity, from
  any station (local Assembly In scans and imported QC scans).
- `kickback` = every UNDO that actually voided a scan (the local Assembly In card,
  and the brief's QC `undone` outcome).

The brief needs no change, because its `undone` record already carries the drawer
id.
