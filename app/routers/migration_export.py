"""Eagle-vm migration (2026-10): one-off export of the live data off Render.

Render gives no file access to its persistent disk without a dashboard step, so
these two GETs hand over a consistent copy of the database and a tarball of the
photo folder to whoever holds RELAY_API_KEY (production count's existing secret -
no new key to provision). Both are exempted from the login in
app/auth_middleware.py PUBLIC_EXACT_PATHS, same discipline as the relay ingest
paths: a machine caller, protected by its own header check instead.

Delete this router once the app runs on eagle-vm and Render is suspended - the VM
has its own nightly snapshot (deploy/backup_snapshot.py), so nothing needs it after.
"""

from __future__ import annotations

import os
import sqlite3
import tarfile
import tempfile
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.config import get_settings
from app.routers.sync import _verify_relay_key

router = APIRouter(prefix="/api/v1/sync/export", tags=["sync"])

DATABASE_PATH = "/api/v1/sync/export/database"
UPLOADS_PATH = "/api/v1/sync/export/uploads"


def _sqlite_file() -> Path:
    url = get_settings().database_url
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise HTTPException(status_code=500, detail="Export only supports a SQLite database.")
    return Path(url[len(prefix) :])


def _temp_path(suffix: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return Path(name)


@router.get("/database")
def export_database(x_relay_key: str | None = Header(default=None)) -> FileResponse:
    """A consistent snapshot via SQLite's online-backup API (safe while the app is
    writing), checked with PRAGMA integrity_check before it is handed over. Built in
    the system temp dir, not on the 1 GB data disk, and deleted after sending."""
    _verify_relay_key(x_relay_key)
    dest = _temp_path(".sqlite")
    src = sqlite3.connect(_sqlite_file())
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
        result = out.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        out.close()
        src.close()
    if result != "ok":
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Snapshot failed integrity_check: {result}")
    return FileResponse(
        dest,
        media_type="application/vnd.sqlite3",
        filename="defect_tracker.db",
        background=BackgroundTask(dest.unlink, missing_ok=True),
    )


@router.get("/uploads")
def export_uploads(x_relay_key: str | None = Header(default=None)) -> FileResponse:
    """Every file in UPLOADS_DIR as a .tar.gz (paths relative to the folder)."""
    _verify_relay_key(x_relay_key)
    dest = _temp_path(".tar.gz")
    uploads = get_settings().uploads_dir
    with tarfile.open(dest, "w:gz") as tar:
        if uploads.is_dir():
            for path in sorted(uploads.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=path.relative_to(uploads).as_posix())
    return FileResponse(
        dest,
        media_type="application/gzip",
        filename="uploads.tar.gz",
        background=BackgroundTask(dest.unlink, missing_ok=True),
    )
