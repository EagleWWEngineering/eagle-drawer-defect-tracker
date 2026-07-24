# User Guide

How to use the Eagle Drawer Defect Tracker day to day. For setup/installation, see
the main [`README.md`](../README.md). For the business rules behind these screens,
see [`PROJECT_SPEC.md`](PROJECT_SPEC.md).

## Roles (prototype only — not security)

The role selector in the top-right corner (QC / Manufacturing Engineer / Admin) only
labels who did what in the audit log for this single-user pilot. It does not restrict
what you can click — anyone can reach any screen. Real access control is a
pre-requisite before this app is used on a shared network (see `PROJECT_SPEC.md`
section 8).

## Daily QC workflow

1. **Record production counts once per shift** on the **Daily Summary** screen:
   drawers inspected, unique drawers rejected, drawers reworked, drawers scrapped.
   This is the denominator behind every rate on the Dashboard — do it even on a day
   with zero defects.
2. **Log each defect as you find it** on the **New Defect** screen (aim for
   30–45 seconds per entry):
   - Work order number (required — this is the only required ID).
   - Found station (where you found it) and, if you suspect where it came from,
     Possible source station — this is a hypothesis, not a confirmed cause.
   - Priority (Urgent/High/Normal) — always shown with a text label, never color alone.
   - One or more defect categories. If one drawer has three sanding scratches, that's
     still one "Sanding / Surface" row — just leave the quantity at 1. If three
     separate drawers all have the same sanding defect, set the quantity to 3 on that
     one row instead of adding three rows.
   - A photo is optional and can be attached right after saving.
3. You'll see a confirmation with the case number (e.g. `DF-20260724-0001`) —
   write that on the paper log if you're using one, or read it back to whoever is
   collecting paper logs that day.
4. **Re-inspection of an unresolved defect** (same drawer, same problem, still not
   fixed): don't create a new entry. Find the existing case (Reports → work order
   search, or the Rework Queue) and update its status/notes there instead.

## Using a paper log first

If you're on the floor without a screen: fill out the **Print Log** screen's printable
form as defects are found, then have someone type it into **New Defect** later the
same day, using the paper log's date as the Production Date (New Defect lets you pick
a date in the past for exactly this reason).

## Moving a case through rework

On the **Rework Queue** screen (sorted Urgent → High → Normal, oldest first within
each priority):
- Pick a new status from the dropdown next to a case — only statuses that are
  actually allowed from the current status are offered.
- Choosing a disposition (Rework/Scrap/Use As Is/Hold) will typically move the status
  along with it (Rework → In Rework, Hold → Waiting, Scrap → Closed – Scrapped,
  Use As Is → Closed – Use As Is).
- Reopening a case that's already closed requires a note explaining why — this is
  intentional, so there's always a record of why a "done" case came back.

## Reviewing quality data (Manufacturing Engineer)

The **Dashboard** and **Reports** screens both show:
- KPI cards: drawers inspected, defect events, unique drawers rejected, defects per
  100 drawers, rejection rate, first pass yield, rework rate, scrap rate. Any rate
  shows "N/A" instead of a number when drawers inspected is zero for that period.
- A **Pareto chart** by defect category (or, switch the dropdown to group by possible
  source station — remember, that's a hypothesis, not a confirmed root cause).
  Clicking a bar/row on the Reports page filters the record table below it to just
  that group.
- A **trend chart** by day or week.
- A **work order history** lookup, showing every case ever logged against one work
  order.

**Reports → Export CSV** downloads exactly what's currently filtered, with raw counts
and identifiers (not just percentages) — open it in Excel for deeper analysis.

## Admin: managing stations and categories

The **Admin** screen lets you rename, reorder, and activate/deactivate stations and
defect categories. Deactivating hides something from new-entry dropdowns without
deleting its history — anything already referenced by a real defect case stays
intact and visible in reports.

## Backing up your data

Run `python scripts/backup_database.py` any time (the app can keep running while you
do this — it uses SQLite's official online backup method, not a raw file copy).
Backups land in `data/backups/`, timestamped, with the 20 most recent kept by default.
