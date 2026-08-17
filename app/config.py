"""Application configuration, loaded from environment variables / .env."""

from __future__ import annotations

import decimal
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


class Settings:
    """Central place for every configurable value in the app.

    Keeping this in one small class means a beginner only has one file to check
    when asking "where does this setting come from?".
    """

    def __init__(self) -> None:
        self.database_url: str = os.getenv(
            "DATABASE_URL", f"sqlite:///{(PROJECT_ROOT / 'data' / 'defect_tracker.db').as_posix()}"
        )
        self.app_host: str = os.getenv("APP_HOST", "127.0.0.1")
        self.app_port: int = int(os.getenv("APP_PORT", "8000"))
        self.display_timezone: str = os.getenv("DISPLAY_TIMEZONE", "America/New_York")
        self.max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "8"))
        # Defaults to a folder inside the project (unchanged local-dev behavior).
        # On Render, UPLOADS_DIR is set to a subdirectory of the mounted persistent
        # disk (see render.yaml) - without this override, uploaded photos would live
        # on the container's ephemeral filesystem and be wiped on every redeploy.
        self.uploads_dir: Path = Path(os.getenv("UPLOADS_DIR", str(PROJECT_ROOT / "uploads")))
        self.data_dir: Path = PROJECT_ROOT / "data"
        self.defect_api_url: str = os.getenv("DEFECT_API_URL", "http://127.0.0.1:8000")
        # Temporary local-testing default while a local production brief instance is
        # being used for development. The real production address
        # (http://20.62.194.32:8094) will be set via PRODUCTION_BRIEF_URL in the real
        # .env file later - nothing in this codebase should ever hardcode either
        # address outside of this one default.
        self.production_brief_url: str = os.getenv(
            "PRODUCTION_BRIEF_URL", "http://127.0.0.1:8094"
        ).rstrip("/")
        self.sync_interval_minutes: int = int(os.getenv("SYNC_INTERVAL_MINUTES", "60"))
        # Separate secret from APP_USERNAME/APP_PASSWORD_HASH (the human shared
        # login), checked by POST /api/v1/sync/customer-issues/ingest-raw via the
        # X-Relay-Key header (see app/routers/sync.py). Deliberately independent so
        # a human password rotation never breaks the automated local relay script
        # (scripts/relay_customer_issues.py) and vice versa. Empty by default -
        # an unset key means the endpoint rejects every request (see
        # _verify_relay_key), never accepts one by accident.
        self.relay_api_key: str = os.getenv("RELAY_API_KEY", "")
        # Seed value only (app/seed_data.py writes this into the app_settings table
        # once, on first run). After that, the DB row is authoritative and editable
        # via Admin - see app/services/settings_service.py. Changing this env var
        # later has no effect on an already-seeded database.
        self.default_cost_per_drawer: decimal.Decimal = decimal.Decimal(
            os.getenv("DEFAULT_COST_PER_DRAWER", "35.00")
        )

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
