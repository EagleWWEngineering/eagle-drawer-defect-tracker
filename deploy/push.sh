#!/usr/bin/env bash
#
# Deploy this app to eagle-vm. Modeled on eagle-drawers-production-count's
# deploy/push.sh (which is modeled on eagle-production-brief's):
#   - ships a `git archive` of HEAD (tracked files only) -- never the working copy --
#     into a staging dir, then the VM's own rsync syncs it to live
#   - never touches .env (secrets) or the data, which lives OUTSIDE the deploy dir
#     (DATA_DIR: the database and the photo folder) so `rsync --delete` can never
#     reach it
#   - stamps VERSION so /api/v1/health's git_commit shows which commit is running
#   - restarts ONLY this app's own user service, then refuses to call it done unless
#     the service really restarted and /api/v1/health reports the commit just shipped
#
# The app listens on 127.0.0.1:8112 only. Shop computers reach it through
# production count's /defects/* pass-through (http://20.62.194.32:8105/defects/);
# production count's feeds call it directly on the loopback port.
#
# Usage: deploy/push.sh            (refuses a dirty tree or unpushed commits)
set -euo pipefail

HOST="${DEPLOY_HOST:-eagle-vm}"
DEST="/home/eagleagent/eagle-drawer-defect-tracker"
DATA_DIR="/home/eagleagent/eagle-drawer-defect-tracker-data"
UNIT="eagle-drawer-defect-tracker.service"
BACKUP_UNIT="eagle-drawer-defect-tracker-backup"
HEALTH_URL="http://127.0.0.1:8112/api/v1/health"
SNAPSHOT_SH="/home/eagleagent/bin/snapshot_release.sh"
REPO_NAME="eagle-drawer-defect-tracker"

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
qq() { printf '%q' "$1"; }

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "ERROR: uncommitted changes -- commit (and push) what you're deploying first." >&2
  git status --short --untracked-files=no >&2
  exit 1
fi
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git fetch -q origin "$BRANCH"
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/$BRANCH")" ]]; then
  echo "ERROR: HEAD differs from origin/$BRANCH -- git pull / git push first." >&2
  exit 1
fi

SHA="$(git rev-parse HEAD)"
echo "==> Deploying $SHA ($BRANCH) -> $HOST:$DEST"

if ssh "$HOST" "test -x $(qq "$SNAPSHOT_SH") && test -d $(qq "$DEST")"; then
  echo "==> Snapshot (rollback point): $(ssh "$HOST" "$(qq "$SNAPSHOT_SH") snapshot $(qq "$REPO_NAME")" || echo none)"
fi

STAGE="/tmp/$REPO_NAME.deploy.$$"
git archive --format=tar HEAD | ssh "$HOST" "set -e
  rm -rf $(qq "$STAGE") && mkdir -p $(qq "$STAGE")
  tar xf - -C $(qq "$STAGE")
  printf '%s\n' $(qq "$SHA") > $(qq "$STAGE/VERSION")"

BEFORE="$(ssh "$HOST" "XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user show -p ActiveEnterTimestampMonotonic --value $UNIT 2>/dev/null || echo 0")"

ssh "$HOST" "set -e
  export XDG_RUNTIME_DIR=/run/user/\$(id -u)
  mkdir -p $(qq "$DEST") $(qq "$DATA_DIR/uploads")
  test -f $(qq "$DEST/.env") || { echo 'ERROR: $DEST/.env missing -- create it first.' >&2; exit 1; }
  rsync -a --delete \
    --exclude '.env' --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '*.db' --exclude '*.db-*' --exclude '*.log' --exclude '/data/' --exclude '/uploads/' \
    $(qq "$STAGE/") $(qq "$DEST/")
  rm -rf $(qq "$STAGE")
  cd $(qq "$DEST")
  [ -d .venv ] || python3.12 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q .
  mkdir -p ~/.config/systemd/user
  cp deploy/$UNIT deploy/$BACKUP_UNIT.service deploy/$BACKUP_UNIT.timer ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable $UNIT >/dev/null
  systemctl --user enable --now $BACKUP_UNIT.timer >/dev/null
  systemctl --user restart $UNIT"

echo "==> Waiting for the restarted service to report $SHA"
for _ in $(seq 1 30); do
  sleep 2
  AFTER="$(ssh "$HOST" "XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user show -p ActiveEnterTimestampMonotonic --value $UNIT")"
  HEALTH="$(ssh "$HOST" "curl -s --max-time 3 $HEALTH_URL" || true)"
  if [[ "$AFTER" != "$BEFORE" && "$AFTER" != 0 && "$HEALTH" == *"$SHA"* && "$HEALTH" == *'"database":"ok"'* ]]; then
    echo "==> OK: $HEALTH"
    exit 0
  fi
done
echo "ERROR: service did not come up healthy on $SHA. Last /health: ${HEALTH:-<none>}" >&2
ssh "$HOST" "XDG_RUNTIME_DIR=/run/user/\$(id -u) journalctl --user -u $UNIT -n 30 --no-pager" >&2 || true
exit 1
