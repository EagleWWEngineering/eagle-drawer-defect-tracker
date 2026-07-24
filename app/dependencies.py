"""Shared FastAPI dependencies.

PROTOTYPE NOTE (PROJECT_SPEC.md section 8): get_actor_role reads a plain header with
no verification. It exists only so the audit log can record "who chose what role in
the UI" for a single-user local pilot. It is NOT authentication or authorization.
Before any LAN or multi-user deployment, replace this with real auth.
"""

from __future__ import annotations

from fastapi import Header

from app.database import (
    get_db,
)  # re-exported for convenience: `from app.dependencies import get_db`

__all__ = ["get_db", "get_actor_role"]


def get_actor_role(x_actor_role: str | None = Header(default=None)) -> str:
    return x_actor_role or "QC"
