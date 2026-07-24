"""Append-only audit logging (PROJECT_SPEC.md section 8).

Every create/edit/status change/soft delete/export/master-data change/MCP write
must call record() so there is one durable trail for all of them.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models import AuditLog


def _to_json(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)


def record(
    db: Session,
    *,
    actor_role: str,
    action: str,
    entity_type: str,
    entity_id: str | int | None,
    inputs: dict | None = None,
    before: dict | None = None,
    after: dict | None = None,
    success: bool = True,
    message: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_role=actor_role,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        inputs_json=_to_json(inputs),
        before_json=_to_json(before),
        after_json=_to_json(after),
        success=success,
        message=message,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
