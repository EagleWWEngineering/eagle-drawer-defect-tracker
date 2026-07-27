"""Application configuration, loaded from environment variables / .env."""

from __future__ import annotations

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
        self.uploads_dir: Path = PROJECT_ROOT / "uploads"
        self.data_dir: Path = PROJECT_ROOT / "data"
        self.defect_api_url: str = os.getenv("DEFECT_API_URL", "http://127.0.0.1:8000")
        self.production_brief_url: str = os.getenv(
            "PRODUCTION_BRIEF_URL", "http://20.62.194.32:8094"
        ).rstrip("/")
        self.sync_interval_minutes: int = int(os.getenv("SYNC_INTERVAL_MINUTES", "60"))

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
