# RESUME: where eagle-drawer-defect-tracker stands

Last updated 2026-10-07. Hand-off for the next session. The redesign and the move to
eagle-vm are in [`docs/PROJECT_SPEC_PHASE12.md`](docs/PROJECT_SPEC_PHASE12.md); the
baseline is [`docs/TECHNICAL_SUMMARY_2026-10-01.md`](docs/TECHNICAL_SUMMARY_2026-10-01.md).

## State right now

- **Live on eagle-vm since 2026-10-06 14:11 ET** (off Render): shop address
  `http://20.62.194.32:8105/defects/` (through production count), service on
  127.0.0.1:8112, no login. Running branch `vm-migration` (30fea93 + photo-retention
  commit). Render is still up but receives nothing - Rodolfo suspends it once the
  brief's PR #37 (defect panel URL) is deployed.
- **All five production-count feeds land on the VM** (verified 10-06).
- **Redesign built, NOT deployed**: branch `redesign-2026-10` (pushed), 793 tests.
  Waiting for Rodolfo's review on the laptop copy, then one deploy.
- Production count master has two undeployed commits for the redesign: customer
  name in the order-lines feed (39ce46f) and clamp on drawer events (4de3ab5). Both
  are safe in either order (the live tracker ignores unknown keys).

## Deploying the redesign (after Rodolfo says go)

1. Tracker: merge `redesign-2026-10` into master, push, `bash deploy/push.sh`
   (runs the 5 migrations on start; `alembic upgrade head` is ExecStartPre).
2. Production count: `git push origin master` (already pushed) + `bash deploy/push.sh`.
3. Check: `/defects/` pages, `/api/v1/sync/health` all ok, a customer name on the
   queue after the next :30 order-lines push.
4. Rodolfo sets the dashboard target in Admin; shop TV gets `/defects/?tv=1`.

## How to operate it

- **Tests:** `.venv/Scripts/python.exe -m pytest -q`, `ruff check .`, `ruff format --check .`.
- **Deploy:** commit, push the branch, `bash deploy/push.sh`.
- **Schema change:** batch-mode Alembic migration, tested up/down/up on a copy of
  the live DB (`scp eagle-vm:eagle-drawer-defect-tracker-data/defect_tracker.db`).
- **Secrets:** on the VM in `~/eagle-drawer-defect-tracker/.env` (relay key =
  production count's DEFECT_TRACKER_RELAY_KEY; brief key = the brief's
  DEFECT_TRACKER_API_KEY). Never printed.
- **Defect categories are Rodolfo's.** Never create, rename or seed them in code.

## Open items

1. Review of the redesign (Rodolfo).
2. Brief PR #37, then suspend Render; later delete it and remove
   `app/routers/migration_export.py`.
3. eagle-ops `services.toml` entry for the tracker (Blake).
4. Optional: kickbacks from the on-screen Undo / admin voids; Slack alert for late
   feeds (channel undecided).
5. 2026-09-30 and 10-01 completed counts look low (31 and 12) - partial counts from
   production count's first days; they spike the trend.
