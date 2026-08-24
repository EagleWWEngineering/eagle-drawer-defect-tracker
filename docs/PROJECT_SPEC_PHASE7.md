# Phase 7 Addendum — Cost Model + Disposition/Status Simplification

Addendum to [`PROJECT_SPEC.md`](PROJECT_SPEC.md) and the Phase 2–6 addenda.
Everything in all of them still applies unchanged except where this document
says otherwise. This is a **vocabulary and display** change plus one narrow,
logged migration of *currently-open* cases. **No table drops, no column drops,
no destructive backfills** — historical rows keep their stored `status` and
`disposition` values verbatim, including values retired by this change.

## Dispositions: two options

```python
VALID_DISPOSITIONS = ["Rework", "Set Aside"]
```

Retired for new entry: `Use As Is`, `Hold`, `Scrap` — still valid on historical
rows, never written by new code. The "More options..." disclosure that used to
hide Scrap is gone from the New Defect form; with two options it was pure noise.

| Disposition | Meaning | Where it goes |
|---|---|---|
| **Rework** | Being worked on right now, at the station | Quick-closes on the spot (default flow) |
| **Set Aside** | Defect exists, waiting to be worked | Always `Open`, no instant-close path |

Both count as **+1 defect** — the counting rules in `PROJECT_SPEC.md` §2 are
unchanged (one category on one case = one defect event; one `DefectCase` = one
defective drawer; duplicate categories still merge).

## Statuses: three options

```python
VALID_STATUSES = ["Open", "Closed - Repaired", "Closed - Use As Is"]
```

```
Open                  → Closed - Repaired, Closed - Use As Is
Closed - Repaired     → (terminal)
Closed - Use As Is    → (terminal)
```

Retired for new entry: `In Rework`, `Waiting`, `Ready for QC Recheck`,
`Closed - Scrapped`. They remain valid **stored** values — a historical case in
one of them still renders, filters, and exports correctly — but no code path
writes any of them for a new case or a new status change.

- Reopening a closed case → `Open` still **requires a note**.
- A normal close does not require a note.
- There is no more generic "move to an intermediate open status" transition at
  all — every entry in `STATUS_TRANSITIONS` is now empty. The only two
  transitions that exist are direct-close (`direct_close_statuses`) and reopen
  (the `is_reopen` check in `update_case_status`).
- `direct_close_statuses()`'s *source* set (`DIRECT_CLOSE_SOURCE_STATUSES`)
  defensively still includes the retired open-ish statuses, so a stray case
  that somehow still carries one is never stuck without a close action — but
  its *target* set (`NEW_CLOSE_STATUSES`) is only ever the two new closed
  statuses; nothing closes a case into `Closed - Scrapped` anymore.
- `CLOSED_STATUSES` (used for `is_reopen` / cost-bucket checks) still includes
  the retired `Closed - Scrapped`, so an old scrapped case is still recognized
  as closed and can still be reopened.

### New Defect form entry points

- Disposition **Rework** + a repair action → creates directly as
  `Closed - Repaired` (default, pre-selected).
- Disposition **Rework** + outcome "Use As Is" → creates directly as
  `Closed - Use As Is`. This is how "defect exists, we're shipping it as is"
  gets recorded now that Use As Is is no longer its own disposition — see
  `create_defect_case`'s `instant_close_outcome` param
  (`INSTANT_CLOSE_OUTCOMES` = `{"Repaired": "Closed - Repaired", "Use As Is":
  "Closed - Use As Is"}`, default `"Repaired"`). **This entry point was a
  DECISION FLAG at the time it shipped** — Rodolfo cut Use As Is as a
  *disposition* but kept it as a *status*, so it needed some way to reach it at
  creation time; if he'd rather it only be reachable by closing a queued case,
  delete this branch (`instant_close_outcome`, the outcome toggle on the New
  Defect form, and `INSTANT_CLOSE_OUTCOMES["Use As Is"]`).
- Disposition **Set Aside** → creates as `Open`, lands in the queue. No instant
  close for Set Aside — it means "waiting to be worked", the opposite of
  "already done".
- Every non-instant-close case lands on **`Open`** now, regardless of which
  disposition was chosen — there's no more separate `In Rework`/`Waiting` queue
  status to route into. `disposition` on an open case now records *why* it's
  open (being worked vs. waiting), not *where* it is.

`resolved_on_the_spot` keeps its current meaning, set once at creation.
`skipped_recheck` is retired — the column and its historical `True`/`False`
values stay on old rows, but `update_case_status` no longer writes to it (no
recheck status exists to have "skipped").

## Data migration (the only data change)

Two migrations: `3d8532f3a9ec` (schema: adds `defect_cases.cost_per_drawer_at_time`,
nullable) and `7c1f9a2b4e6d` (data: the retired-status/disposition backfill).

Only **currently-open** (non-`Closed - *`) cases are touched, and only if they
actually carry a retired value:

- status `In Rework` / `Waiting` / `Ready for QC Recheck` → `Open`, with a real
  `status_history` row (`from_status` = the old value, `to_status` = `"Open"`,
  a note explaining the schema change) and an `audit_log` row.
- disposition `Hold` / `Scrap` / `Use As Is` (on any of those same non-closed
  cases, independent of whatever their status is/was) → `Set Aside`, logged
  the same way (no `status_history` row for a disposition-only change — that
  table records status transitions specifically; the `audit_log` row is what
  makes a pure disposition remap traceable).
- **Closed cases are never touched** — not their status, not their
  disposition, not `Closed - Scrapped`, nothing. Verified in
  `tests/unit/test_phase7_migration.py` with a seeded closed case that
  literally carries a retired disposition (`Closed - Repaired` / `Hold`) and
  comes out byte-identical.
- The migration is idempotent: re-running `alembic upgrade head` (e.g. on a
  redeploy) finds nothing left to migrate and writes nothing new.
- `downgrade()` is a deliberate no-op — multiple old statuses collapse onto
  `Open`, so there's no way to recover which one a case used to have from the
  data alone; the `status_history` rows this migration writes ARE that record,
  and deleting them to "undo" the migration would itself be a destructive
  rewrite this phase's non-negotiable rule forbids.

Real production data at the time this shipped: 2 cases moved from `In Rework`
to `Open` (both kept disposition `Rework`, not retired); 8 already-`Closed -
Repaired` cases carrying disposition `Hold` were confirmed untouched.

## Cost model

> Every defect case carries exactly one unit of `cost_per_drawer`, snapshotted
> at creation. A case closed `Closed - Use As Is` carries zero.

- `DefectCase.cost_per_drawer_at_time` (`Numeric(10,2)`, nullable): the
  Admin-tab rate active at creation, snapshotted the same way
  `DailyProductionSummary.cost_per_drawer_at_time` already was. Rate changes
  never re-price history. A case that predates this column (null) falls back
  to the *currently-configured* rate at read time — see
  `metrics_service.compute_case_cost` — so historical cost never silently
  becomes $0.
- **One unit per case** — never multiplied by `DefectItem` count or
  `affected_drawer_quantity`. A case is one defective drawer no matter how many
  categories/items are on it (`PROJECT_SPEC.md` §2).
- **Open cases count immediately.** Cost is removed only if/when the case
  closes `Closed - Use As Is`.
- **Historical `Closed - Scrapped` cases count normally**, as one unit — Scrap
  stays completely out of KPIs/reporting (no scrap rate, no scrap cost line,
  no scrap field anywhere), but it isn't specially zeroed in the cost sum
  either; only `Closed - Use As Is` is.
- **Cost Avoided** (new): the summed snapshot cost of every case in the
  filtered range that closed `Closed - Use As Is` — displayed alongside Total
  Internal Quality Cost, not subtracted from it. `total_internal_quality_cost`
  is unaffected by `cost_avoided`; they're reported side by side.

Deleted entirely: `sum_internal_rework_cost`, `defect_case_derived_rework_count`,
`classify_case_cost_bucket`, and the old dual-source
`compute_internal_quality_cost` signature (`daily_summary_entries` /
`fallback_case_rework_count` / `has_daily_summary_rows` / `cost_basis`). The new
`compute_internal_quality_cost(cases, *, fallback_rate)` takes a flat list of
`(status, cost_per_drawer_at_time)` — one entry per distinct case in the
filtered range — and returns `{"internal_rework_cost", "cost_avoided"}`.
`DailyProductionSummary.cost_per_drawer_at_time` stays in the schema with its
historical values; nothing reads it for cost anymore.

### CSV export

`day_cost_per_drawer` / `day_internal_rework_cost` (date-joined from
`DailyProductionSummary`) are replaced by **per-case** columns:
`case_cost_per_drawer`, `case_internal_cost`, `case_cost_avoided` — computed
straight from each row's own case, since cost no longer depends on whether a
Daily Production Summary exists for that date at all.

### `DailyProductionSummaryOut`

The `internal_rework_cost` / `internal_scrap_cost` computed fields were
**removed** — they multiplied `drawers_reworked`/`drawers_scrapped` by the
row's rate snapshot, which is exactly the retired per-date model. Keeping them
would have shown a real dollar figure with nothing to do with the actual
reported cost anymore. `cost_per_drawer_at_time` itself stays as a plain field
(informational: what rate was active when this row was saved). The Daily
Summary form's "Cost for this entry" post-save card and the "Rework cost"
column on its Recent Entries table were removed along with it — this wasn't
explicitly asked for, but followed directly from deleting the fields those
displays depended on.

## KPI fallout

| KPI | Change |
|---|---|
| Defects per 100 Drawers | unchanged |
| Drawer Rejection Rate | unchanged |
| First Pass Yield | unchanged |
| **Rework Rate** | redefined: `(cases with disposition "Rework" in range) / drawers_inspected × 100` — no status qualifier, no more reading `DailyProductionSummary.drawers_reworked` |
| Total Internal Quality Cost | = new case-derived cost |
| Quality Cost per Drawer Inspected | unchanged formula, new numerator |
| **Cost Avoided** | new |
| % Resolved On The Spot | unchanged |
| % Queued Rework Closed Without Recheck | **removed** — no recheck status exists |

`KpiOut.drawers_reworked` is a repurposed field, not a removed one: it now
holds the count of Rework-dispositioned cases (Rework Rate's numerator), not a
`DailyProductionSummary` sum. Kept under the same name for API/schema
stability. `defect_case_rework_count` and `cost_basis` were removed along with
the dual-source cost model they described — there's only one cost source now,
so there's nothing left to disambiguate.

**Daily Production Summary form:** the `drawers_reworked` input was removed
(**a DECISION FLAG, not something Rodolfo asked for explicitly** — it follows
from the cost/Rework-Rate change: a second hand-entered rework number next to
a case-derived one is a contradiction waiting to happen, and fewer operator
fields is the standing design principle). The column and its historical values
stay in the database; `DailyProductionSummaryIn.drawers_reworked` is now
`int | None = None`, following the exact same "omit to preserve, explicit int
to override" pattern `drawers_scrapped` already used since Phase 4 (this
applies to the MCP `record_daily_production` tool too — its default changed
from `0` to `None` so an unset argument no longer silently zeroes an existing
value). `drawers_rejected_unique` stays on the form, still driving Rejection
Rate and First Pass Yield.

## MCP server

`mcp_server/server.py` talks to the same REST API and inherits its validation
automatically — most of Change 7 was docstring accuracy, not new logic:
`record_defect_case`'s disposition docs now list `"Rework"`/`"Set Aside"`;
`update_defect_case_status`'s docs list the three settable statuses and note
that a retired legacy value may still be *displayed* on old data;
`get_defect_summary`'s docs describe the new cost/Rework Rate shape (and no
longer claim a `scrap_rate`/`internal_scrap_cost` that never actually existed
in the response even before this phase — a pre-existing doc inaccuracy fixed
here since this phase touches exactly that KPI surface). MCP still cannot
write anything the web UI can't: it doesn't expose `resolved_on_the_spot` /
`instant_close_outcome` at all (never did), so an MCP-created case is always
`Open`, same as choosing "leave this case open" in the UI.

## Unchanged (verified, not "fixed")

Defect categories, found station, possible source station, priority, notes,
photos, the customer-issue vocabulary and sync (Phase 2/3), the Phase 6
schedule feature, the audit log, and the duplicate station display in the New
Defect form (intentional, per `CLAUDE.md`) — none of these were touched.

## Testing

`tests/unit/test_status_transitions.py` and `test_resolved_on_the_spot.py`
were substantially rewritten (they encode the exact behavior this phase
redefines); `test_cost_tracking.py` and `test_cost_tracking_api.py` likewise,
for the new cost model. `tests/unit/test_phase7_migration.py` is new: it runs
the real `alembic upgrade head` (via subprocess, against a throwaway SQLite
file — `env.py` re-reads `DATABASE_URL` from the process environment, so an
in-process fixture can't drive it) against a seeded database covering every
retired status/disposition combination, including closed cases that already
carry a retired disposition, and asserts the exact byte-identical/migrated
split described above, plus idempotency on a second run.
