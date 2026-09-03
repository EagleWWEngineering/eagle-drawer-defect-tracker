"""Application configuration, loaded from environment variables / .env."""

from __future__ import annotations

import decimal
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env_bool(name: str, default: bool) -> bool:
    """Phase 8a: the only boolean env var this app reads (everything else so far
    has been a string/int/Decimal) - a small dedicated parser rather than a bare
    `bool(os.getenv(...))`, which would treat the literal string "false" as truthy.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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
        # Brief Export (Part A): checked against the X-Brief-Key header on
        # GET /api/v1/brief/summary (see app/routers/brief.py). A separate secret
        # from RELAY_API_KEY above - different machine (the production brief's VM,
        # pulling FROM this app), opposite direction, so revoking one must never
        # break the other. Empty by default - an unset key means the endpoint
        # rejects every request (see brief.py's _verify_brief_key), never accepts
        # one by accident.
        self.brief_api_key: str = os.getenv("BRIEF_API_KEY", "")
        # Seed value only (app/seed_data.py writes this into the app_settings table
        # once, on first run). After that, the DB row is authoritative and editable
        # via Admin - see app/services/settings_service.py. Changing this env var
        # later has no effect on an already-seeded database.
        self.default_cost_per_drawer: decimal.Decimal = decimal.Decimal(
            os.getenv("DEFAULT_COST_PER_DRAWER", "35.00")
        )
        # Label-scan OCR (PROJECT_SPEC_PHASE9.md Part 3). Deliberately ships LIVE
        # by default, unlike every other optional feature in this app: the default
        # engine (OCR_PROVIDER="tesseract") runs entirely in the browser, costs
        # nothing, sends no data anywhere, and needs no credential - there is
        # nothing to ship dormant, because there is nothing that can incur a bill
        # or fail on a missing key. Only the cloud providers (azure/google/
        # anthropic) need OCR_API_KEY - see app/routers/scan.py, which still 503s
        # for those if a key isn't configured. OCR_ENABLED=false remains a full
        # kill switch (QR decoding and manual entry keep working regardless).
        self.ocr_enabled: bool = _env_bool("OCR_ENABLED", True)
        self.ocr_provider: str = os.getenv("OCR_PROVIDER", "tesseract")
        self.ocr_endpoint: str = os.getenv("OCR_ENDPOINT", "")
        self.ocr_api_key: str = os.getenv("OCR_API_KEY", "")
        # Phase 3 (favorites quick-pick bars). Unlike OCR_ENABLED above, this
        # defaults FALSE: favorites visibly changes the New Defect form's layout
        # (a new bar above Found Station / Possible Source / the category grid),
        # where OCR_ENABLED's default-on engine runs invisibly in the background
        # with no UI of its own. Ships dormant so a deploy alone never changes
        # what the shop floor sees - it's a deliberate opt-in via this env var
        # once Admin has actually favorited something, not an instant-on feature.
        self.favorites_enabled: bool = _env_bool("FAVORITES_ENABLED", False)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
