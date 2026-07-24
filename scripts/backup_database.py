#!/usr/bin/env python
"""Create a timestamped, consistent backup of the SQLite database.

Uses Python's built-in sqlite3 online backup API (Connection.backup()), which is
safe to run while the app is live (including in WAL mode) - it does not just copy
the file bytes, which could capture a half-written page.

Usage:
    python scripts/backup_database.py [--keep N]

Writes to data/backups/defect_tracker_YYYYMMDD_HHMMSS.db and, by default, keeps the
20 most recent backups (older ones are deleted).
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402


def _source_db_path() -> Path:
    settings = get_settings()
    url = settings.database_url
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise SystemExit(f"backup_database.py only supports sqlite:/// URLs, got: {url}")
    return Path(url[len(prefix) :]).resolve()


def backup_database(keep: int = 20) -> Path:
    source_path = _source_db_path()
    if not source_path.exists():
        raise SystemExit(f"Database file not found: {source_path}")

    backups_dir = PROJECT_ROOT / "data" / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_path = backups_dir / f"defect_tracker_{timestamp}.db"

    source_conn = sqlite3.connect(str(source_path))
    dest_conn = sqlite3.connect(str(dest_path))
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        source_conn.close()

    print(f"Backed up {source_path} -> {dest_path}")

    existing = sorted(backups_dir.glob("defect_tracker_*.db"), key=lambda p: p.stat().st_mtime)
    for old in existing[:-keep] if keep > 0 else []:
        old.unlink()
        print(f"Removed old backup: {old.name}")

    return dest_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", type=int, default=20, help="Number of backups to retain (0 = keep all)"
    )
    args = parser.parse_args()
    backup_database(keep=args.keep)


if __name__ == "__main__":
    main()
