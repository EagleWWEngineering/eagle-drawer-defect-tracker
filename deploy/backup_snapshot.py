"""Nightly snapshot of the defect tracker's database and photos into ~/state-backups/local/.

That folder is what the VM's shared off-VM backup (eagle-ops `sql_export_backup.sh`,
03:18 ET, INCLUDE_STATE_BACKUPS=1) tars and uploads to the immutable Azure + Backblaze
vaults. Same arrangement as eagle-drawers-production-count's deploy/backup_snapshot.py.

- Database: SQLite's online-backup API (consistent while the app is writing), written
  to a temp name and renamed, refused if `PRAGMA integrity_check` fails. The existing
  02:00 `eagle-state-backup` job prunes *.sqlite there after 7 days.
- Photos: a .tar.gz of the uploads folder. That prune job only knows *.sqlite, so this
  script deletes its own photo tarballs older than KEEP_DAYS (2 nights: the
  folder is ~0.5 GB, and the off-VM export already keeps every night it uploads).

Stdlib only, so it runs under the system python3.
"""

import datetime
import os
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

DATA_DIR = Path.home() / "eagle-drawer-defect-tracker-data"
SRC_DB = DATA_DIR / "defect_tracker.db"
SRC_UPLOADS = DATA_DIR / "uploads"
OUT_DIR = Path.home() / "state-backups" / "local"
UPLOADS_PREFIX = "eagle_drawer_defect_tracker_uploads."
KEEP_DAYS = 2  # photos never change once taken; 2 nights is ~1 GB at 534 MB each


def snapshot_db(today: datetime.date) -> bool:
    if not SRC_DB.is_file():
        print(f"backup FAILED: database missing: {SRC_DB}", file=sys.stderr)
        return False
    dest = OUT_DIR / f"eagle_drawer_defect_tracker.{today}.sqlite"
    tmp = dest.with_suffix(".sqlite.tmp")
    # Not mode=ro: the DB is in WAL mode (app/database.py), and a read-only connection
    # can't create the -shm file, so it fails whenever the app isn't running.
    src = sqlite3.connect(SRC_DB, timeout=30)
    dst = sqlite3.connect(tmp)
    try:
        with dst:
            src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        dst.close()
        src.close()
    if not ok:
        tmp.unlink(missing_ok=True)
        print("backup FAILED: integrity_check did not return ok", file=sys.stderr)
        return False
    os.replace(tmp, dest)
    print(f"backup OK {dest} ({dest.stat().st_size} bytes)")
    return True


def snapshot_uploads(today: datetime.date) -> bool:
    dest = OUT_DIR / f"{UPLOADS_PREFIX}{today}.tar.gz"
    tmp = dest.with_name(dest.name + ".tmp")
    count = 0
    with tarfile.open(tmp, "w:gz") as tar:
        if SRC_UPLOADS.is_dir():
            for path in sorted(SRC_UPLOADS.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=path.relative_to(SRC_UPLOADS).as_posix())
                    count += 1
    os.replace(tmp, dest)
    print(f"photos OK {dest} ({count} files, {dest.stat().st_size} bytes)")
    return True


def prune_uploads() -> None:
    cutoff = time.time() - KEEP_DAYS * 86400
    for old in OUT_DIR.glob(f"{UPLOADS_PREFIX}*.tar.gz"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
            print(f"pruned {old}")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today()
    db_ok = snapshot_db(today)
    photos_ok = snapshot_uploads(today)
    prune_uploads()
    return 0 if db_ok and photos_ok else 1


if __name__ == "__main__":
    sys.exit(main())
