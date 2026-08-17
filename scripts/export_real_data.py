"""One-time export of real production data from the LOCAL dev SQLite database.

Run directly against the local database (no HTTP involved) using the app's own
SQLAlchemy models/session setup. Produces a single JSON file meant to be POSTed,
by hand, straight to the live Render instance's temporary
`/api/v1/admin/import-data` endpoint (see app/routers/admin.py) - never through
git/GitHub, even briefly, even in a private repo.

Usage:
    python scripts/export_real_data.py [output_path]

If output_path is omitted, the file is written outside the repo (a
"eagle-drawer-export" folder in the current user's home directory) specifically
so it can never be accidentally picked up by `git add .`. A `*.export.json`
.gitignore rule is also in place as a second safeguard in case output_path is
ever pointed inside the repo for convenience.

This script, app/routers/admin.py, and app/services/migration_service.py are all
TEMPORARY - slated for removal once the real migration has been confirmed
successful on the live Render instance. Do not build new functionality on top
of them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running as `python scripts/export_real_data.py` from the repo root
# without having installed the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.services import migration_service  # noqa: E402

DEFAULT_OUTPUT_PATH = Path.home() / "eagle-drawer-export" / "eagle_drawer_real_data.export.json"


def main() -> None:
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    db = SessionLocal()
    try:
        bundle = migration_service.export_bundle(db, settings.uploads_dir)
    finally:
        db.close()

    output_path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")

    defect_cases = bundle["defect_cases"]
    customer_issues = bundle["customer_issues"]
    daily_summaries = bundle["daily_production_summaries"]
    item_count = sum(len(c["items"]) for c in defect_cases)
    photo_count = sum(len(c["photos"]) for c in defect_cases)
    history_count = sum(len(c["status_history"]) for c in defect_cases)

    print(f"Wrote export to: {output_path}")
    print()
    print("Row counts:")
    print(f"  DefectCase:               {len(defect_cases)}")
    print(f"    DefectItem (children):    {item_count}")
    print(f"    DefectPhoto (children):   {photo_count}")
    print(f"    StatusHistory (children): {history_count}")
    print(f"  CustomerIssue:            {len(customer_issues)}")
    print(f"  DailyProductionSummary:   {len(daily_summaries)}")

    missing = bundle["missing_photo_files"]
    if missing:
        print()
        print(f"WARNING: {len(missing)} photo file(s) referenced in the database were not")
        print("found on disk in the uploads directory. Their metadata row was still")
        print("exported (flagged file_missing: true), but no photo bytes for them:")
        for name in missing:
            print(f"  - {name}")

    print()
    print("NEXT STEP: POST this file to the live import endpoint yourself, over HTTPS,")
    print("with your real login. Do NOT commit this file or push it anywhere via git.")


if __name__ == "__main__":
    main()
