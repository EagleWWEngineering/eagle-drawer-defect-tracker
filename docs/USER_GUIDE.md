# User Guide

How to use the Eagle Drawer Defect Tracker day to day. For setup/installation, see
the main [`README.md`](../README.md). For the business rules behind these screens,
see [`PROJECT_SPEC.md`](PROJECT_SPEC.md).

## Logging in

The whole app is behind a single shared username/password (see
`PROJECT_SPEC_PHASE5.md`) — ask whoever set up the app for the shop's shared login
if you don't have it. Once you log in on a device/browser, you stay logged in
indefinitely — there's no "session expired, log in again" surprise. Use **Log out**
on the **Settings** screen to end just your device's session, or **Log out
everywhere** (re-enter the password to confirm) if a device needs to be locked out
immediately, e.g. a shared tablet that's been misplaced.

## Roles (prototype only — not security)

The role selector in the top-right corner (QC / Manufacturing Engineer / Admin) is
completely separate from the login above — it only labels who did what in the audit
log. It does not restrict what you can click once you're logged in — anyone with the
shared login can reach any screen.

## Daily QC workflow

1. **Record production counts once per shift** on the **Daily Summary** screen:
   drawers inspected (type this in), unique drawers rejected, and drawers reworked.
   The latter two are pre-filled as a suggestion computed from the defect cases
   already logged that date — check the number, adjust it if it's not quite right,
   and use **Recalculate from defect cases** if you logged more cases after the
   suggestion first loaded. This is the denominator behind every rate on the
   Dashboard — do it even on a day with zero defects.
2. **Log each defect as you find it** on the **New Defect** screen (target
   15–20 seconds per entry — everything is tap buttons, not dropdowns):
   - Work order number (required — this is the only required ID). Start typing
     and pick from the last 20 work orders if it's one you've already logged
     against; Found Station pre-fills from that work order's last case.
   - Tap the defect category button(s) that apply — multiple is fine, one drawer
     with three sanding scratches is still one "Sanding / Surface" tap.
   - Tap Found Station (where you found it) from the common row, or "More
     stations..." for the full list. Possible Source Station works the same way
     and is optional — it's a hypothesis, not a confirmed cause.
   - Priority defaults to Normal; only tap Urgent or High to override.
   - Disposition (Rework/Scrap/Hold/Use As Is) is optional at entry time — leave
     it blank if you haven't decided yet.
   - Affected drawer quantity defaults to 1 with a +/− stepper.
   - Detected time is a simple `h:mm` field with AM/PM buttons, defaulting to now.
   - Root cause, corrective action, and repair action are **not** on this form —
     fill those in later from the **Rework Queue**, once there's actually
     something to say about them.
   - A photo is optional; attach it from the "Logged this session" list below the
     form after saving, without leaving this screen.
3. After saving you'll see a brief green "Saved: DF-20260724-0001" confirmation.
   The form stays put with the work order number and production date kept (so the
   next entry on the same order/day is a single tap away) and everything else
   cleared, ready for the next entry.
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
- Root cause, corrective action, and repair action live here too, as their own text
  fields per case. They save independently of the status dropdown, so you can jot
  down a root cause on a case that's still Open without having to pick a status
  change at the same time.

## Reviewing quality data (Manufacturing Engineer)

The **Dashboard** and **Reports** screens both show:
- KPI cards: drawers inspected, defect events, unique drawers rejected, defects per
  100 drawers, rejection rate, first pass yield, and rework rate. Any rate shows
  "N/A" instead of a number when drawers inspected is zero for that period. (There
  is no Scrap Rate card — scrap essentially doesn't happen on this floor, so it was
  removed; the underlying data field is still kept for backward compatibility.)
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
