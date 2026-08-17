"""UPLOADS_DIR environment override (app/config.py) - needed so uploaded photos
survive on Render's persistent disk instead of the ephemeral container filesystem
(see render.yaml)."""

from __future__ import annotations

from pathlib import Path

from app.config import PROJECT_ROOT, Settings


def test_uploads_dir_defaults_to_project_uploads_folder(monkeypatch):
    monkeypatch.delenv("UPLOADS_DIR", raising=False)
    settings = Settings()
    assert settings.uploads_dir == PROJECT_ROOT / "uploads"


def test_uploads_dir_can_be_overridden_by_env_var(monkeypatch, tmp_path):
    override = tmp_path / "var-data-uploads"
    monkeypatch.setenv("UPLOADS_DIR", str(override))
    settings = Settings()
    assert settings.uploads_dir == Path(str(override))
