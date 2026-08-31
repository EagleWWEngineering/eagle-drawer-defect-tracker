# Weekend `daily_schedules` Cleanup Runbook

> **Outcome (2026-08-31):** run against the live DB. Step 2's count came back
> **zero** `source='sync'` weekend rows — the relay never actually wrote a bad
> weekend row (or the Phase 2 ingest guard already caught them all before this
> runbook was run). Step 1's inspection found four weekend rows total, all
> `source='manual'`, all `drawers_scheduled = 0`: `2026-08-22`, `2026-08-23`,
> `2026-08-29`, `2026-08-30`. These are correctly excluded from
> `working_days_service`'s working-day set (`source='manual'` and
> `drawers_scheduled = 0`/nothing inspected never counts as a working day
> regardless of source) and were deliberately left in place — the DELETE in
> step 3 is scoped to `source='sync'` only and would never have touched them
> anyway. **Nothing was deleted.** The SQL below is left intact for future use
> if a bad weekend row ever does show up.

Manual cleanup for bad weekend rows in `daily_schedules`, surfaced by the
Working Days Logic (Part C) investigation — the relay used to write Friday's
scheduled count onto Saturday/Sunday dates (see CLAUDE.md's "Working day"
paragraph and `app/services/working_days_service.py`). This is a **human,
by-hand** runbook to be run in Render's Shell against the live database. It is
not automated and nothing in the codebase runs it for you.

**⚠️ Do not run this until the Phase 2 ingest guard
(`app/services/schedule_service.py` `process_schedule_payload`'s
`working_days_service.is_working_day` check) is deployed to production.**
Until that deploy ships, the relay's hourly sync pass will happily recreate
any weekend row you delete here on its very next run — you'd be cleaning up
the same rows every hour instead of once. Confirm the guard is live (check the
deployed commit, or watch `logs/relay_customer_issues.log` on the next hourly
pass for a `not a working day - sync write rejected` line) before touching the
database.

Back up the SQLite database before running any of this, per standing practice,
even though the DELETE below is narrowly scoped.

---

## 0. Connect

In Render's Shell for the `eagle-drawer-defect-tracker` service:

```bash
sqlite3 /var/data/defect_tracker.db
```

(Path is `DATABASE_URL` from `render.yaml` — `sqlite:////var/data/defect_tracker.db`.)

Run the steps below in order. Don't skip the inspection/count steps to get to
the delete faster — they're what let you confirm the blast radius before
anything is removed.

---

## 1. Inspect: weekend rows alongside their preceding Friday

Confirms the actual bug pattern (Saturday/Sunday showing Friday's number
byte-for-byte) before you delete anything. `weekend_scheduled` should equal
`friday_scheduled` for the rows this bug produced — if a row doesn't match,
stop and look at it by hand before including it in the delete below.

```sql
SELECT
    w.production_date                                  AS weekend_date,
    CASE CAST(strftime('%w', w.production_date) AS INTEGER)
        WHEN 6 THEN 'Saturday'
        WHEN 0 THEN 'Sunday'
    END                                                 AS weekend_day_name,
    w.drawers_scheduled                                 AS weekend_scheduled,
    w.source                                             AS weekend_source,
    w.synced_at                                          AS weekend_synced_at,
    date(
        w.production_date,
        CASE CAST(strftime('%w', w.production_date) AS INTEGER)
            WHEN 6 THEN '-1 day'   -- Saturday -> the Friday before it
            WHEN 0 THEN '-2 day'   -- Sunday -> the Friday before it
        END
    )                                                    AS preceding_friday,
    f.drawers_scheduled                                  AS friday_scheduled,
    f.source                                              AS friday_source
FROM daily_schedules w
LEFT JOIN daily_schedules f
    ON f.production_date = date(
        w.production_date,
        CASE CAST(strftime('%w', w.production_date) AS INTEGER)
            WHEN 6 THEN '-1 day'
            WHEN 0 THEN '-2 day'
        END
    )
WHERE CAST(strftime('%w', w.production_date) AS INTEGER) IN (0, 6)
ORDER BY w.production_date DESC;
```

Read through the output. `weekend_scheduled == friday_scheduled` (with
`weekend_source = 'sync'`) is the bug signature and is safe to clean up.
Anything with `weekend_source = 'manual'` is a deliberate overtime-Saturday
entry a human made on the Daily Summary form — it must survive this cleanup
regardless of what its number is or whether it matches the Friday before it.

---

## 2. Blast radius: count before touching anything

Breakdown by weekday and source, so you can see exactly how many rows exist
and confirm none of them are `source = 'manual'` before you delete:

```sql
SELECT
    CASE CAST(strftime('%w', production_date) AS INTEGER)
        WHEN 6 THEN 'Saturday'
        WHEN 0 THEN 'Sunday'
    END        AS weekend_day_name,
    source,
    COUNT(*)   AS row_count
FROM daily_schedules
WHERE CAST(strftime('%w', production_date) AS INTEGER) IN (0, 6)
GROUP BY weekend_day_name, source
ORDER BY weekend_day_name, source;
```

And the exact count the delete in step 3 will remove (`source = 'sync'`
weekend rows only):

```sql
SELECT COUNT(*) AS rows_to_delete
FROM daily_schedules
WHERE CAST(strftime('%w', production_date) AS INTEGER) IN (0, 6)
  AND source = 'sync';
```

If that count is 0, stop here — there's nothing to clean up.

If `weekend_day_name`/`source = 'manual'` shows any rows in the first query
above, **do not** widen the DELETE in step 3 to cover them — they're
legitimate overtime-Saturday entries, not the bug.

---

## 3. Delete — `source = 'sync'` weekend rows only

Run this inside an explicit transaction so you can see the change count
before committing:

```sql
BEGIN TRANSACTION;

DELETE FROM daily_schedules
WHERE CAST(strftime('%w', production_date) AS INTEGER) IN (0, 6)
  AND source = 'sync';
```

`sqlite3` will print something like `changes: 6   total_changes: 6` — check
that number against the `rows_to_delete` count from step 2. If it matches:

```sql
COMMIT;
```

If it doesn't match, or anything looks wrong, roll back instead and stop to
investigate:

```sql
ROLLBACK;
```

The `source = 'sync'` condition is what protects manual rows — this DELETE
can never touch a `source = 'manual'` row no matter what its date is, since
that condition is `AND`ed, not assumed from the weekend filter alone.

---

## 4. Verify afterward

```sql
SELECT production_date, drawers_scheduled, source, synced_at
FROM daily_schedules
WHERE CAST(strftime('%w', production_date) AS INTEGER) IN (0, 6)
ORDER BY production_date DESC;
```

Expected result: empty, or — if any legitimate overtime-Saturday manual
entries exist — only rows with `source = 'manual'`. No `source = 'sync'`
weekend row should remain.

Then confirm the relay isn't recreating them: watch
`logs/relay_customer_issues.log` on the next hourly pass and look for
`not a working day - sync write rejected` for any weekend date it still
scrapes, rather than a new row appearing here again.
