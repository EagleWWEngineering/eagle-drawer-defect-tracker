# RESUME: where eagle-drawer-defect-tracker stands

Last updated 2026-10-01 at `a23e483`. This is the hand-off for the next session. Recent
architecture is in [`docs/TECHNICAL_SUMMARY_2026-10-01.md`](docs/TECHNICAL_SUMMARY_2026-10-01.md);
the full baseline is [`docs/TECHNICAL_SUMMARY_2026-09-03.md`](docs/TECHNICAL_SUMMARY_2026-09-03.md).

## State right now

- **Live on Render**, deployed by pushing `master`. Render runs `alembic upgrade head` on start.
  The head is `c9f4a1e6d3b2`.
- **All data feeds come from production count on eagle-vm:** customer issues, schedule,
  completed drawers, order lines and drawer events. The laptop relay task has been disabled since
  09-30.
- **Unique-ID drawer labels work.** The line letter fills itself and the drawer details show
  (verified on the floor, 09-30).
- **"Save with photo" works,** as does one-tap session-log photos (verified on the floor).
- **UNDO-card kickbacks work** (Phase 11, verified on the floor 10-01):
  - The UNDO card opens a Set Aside case.
  - The next +1 scan closes it as Repaired.
  - The kickback categories are set in Admin.

## How to operate it

- **Tests:** `pytest -q` (719 passing) and `ruff check .`.
- **Deploy:** commit, then `git push origin master`.
- **A schema change** needs a batch-mode Alembic migration. Test it up, down and up again on a
  copy of `data/defect_tracker.db`.
- **Keys:** `RELAY_API_KEY` is in `.env` and matches Render. The `BRIEF_API_KEY` in `.env` is
  stale; use Render's value.
- **Defect categories are Rodolfo's.** Never create, rename or seed them in code. Reference them
  by id through an Admin setting.

## Open items

1. **Photo access for production count** (browse versus showing at the QC station): asked, not
   decided.
2. **Rework Queue auto-refresh:** not requested yet, but kickback cases appear only on reload.
3. **Order-only labels from the Access export:** these carry no drawer id, so they can't
   auto-close (the 09-23 investigation, needs escalation).
