# RESUME: where eagle-drawer-defect-tracker stands

Last updated 2026-10-09 (5b7cf22 deployed 07:18 ET). Hand-off for the next session. The redesign and the move to
eagle-vm are in [`docs/PROJECT_SPEC_PHASE12.md`](docs/PROJECT_SPEC_PHASE12.md); the
baseline is [`docs/TECHNICAL_SUMMARY_2026-10-01.md`](docs/TECHNICAL_SUMMARY_2026-10-01.md).

## State right now

- **Live on eagle-vm since 2026-10-06 14:11 ET** (off Render): shop address
  `http://20.62.194.32:8105/defects/` (through production count), service on
  127.0.0.1:8112, no login. Running branch `vm-migration` (30fea93 + photo-retention
  commit). Render is still up but receives nothing - Rodolfo suspends it once the
  brief's PR #37 (defect panel URL) is deployed.
- **All five production-count feeds land on the VM** (verified 10-06).
- **Redesign LIVE since 2026-10-07 06:27 ET** (77b2c53, migrations to b5e1a7c3d9f2),
  with production count db4ada3 (customer + clamp in the feeds). Pre-deploy backup:
  `~/tmp/tracker-migration/pre-redesign-20261007-0626.sqlite` and the 02:20-style
  snapshot in `~/state-backups/local/`.
- **Kickback station fix (10-07):** QC kickbacks had been filed at hidden "Area 3";
  now chosen in Admin > UNDO Card (QC / Sorting id 16, Assembly id 5); 22 cases moved.

- **10-07 follow-ups (live):** label QR read from a photo (52eb5cc), live iPad QR
  scanning via the https scanner page (3eeb64a), queue closes with two buttons
  Repaired / Use As Is (af87a1f).
- **10-09 (live, 5b7cf22):**
  - New Defect has **no disposition step**: no Rework/Set Aside, no "what was done",
    no leave-open. Every new defect is an open Rework case in the queue (54af2d3).
    Cases close from the queue, or automatically on the drawer's next +1 scan when
    the case was logged by scanning the drawer's unique-ID label.
  - **Slack DM to Rodolfo when a production count feed is late or refused**, one
    more when it is back (`app/services/feed_alert_service.py`, checks every 5 min,
    state in app_settings `feed_alert_open`). VM .env `FEED_ALERT_ENABLED=true`
    (backup `.env.bak-20261009`); Eagle Ops bot via `~/.config/eagle-slack-gateway.env`.
    Test DM sent OK 10-09.
- **Render is suspended** (Rodolfo, 10-09).

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

1. Floor checked by Rodolfo 10-09: New Defect (no disposition) working, drawers register.
2. Later: delete Render and remove `app/routers/migration_export.py`.
3. eagle-ops `services.toml` entry for the tracker (Blake's side).

Dropped 10-09: kickbacks from on-screen Undo / admin voids (not needed); the low
09-30 / 10-01 completed counts (ignore).
