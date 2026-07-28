"""App settings (Phase 4: just cost_per_drawer for now). HTTP input/output only -
business rules (validation, persistence) live in app/services/settings_service.py.
"""

from __future__ import annotations

import decimal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.dependencies import get_actor_role, get_db
from app.schemas import CostSettingsOut, CostSettingsUpdate
from app.services import audit_service, settings_service

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


@router.get("/cost-per-drawer", response_model=CostSettingsOut)
def get_cost_per_drawer(db: Session = Depends(get_db)) -> CostSettingsOut:
    return CostSettingsOut(cost_per_drawer=float(settings_service.get_cost_per_drawer(db)))


@router.put("/cost-per-drawer", response_model=CostSettingsOut)
def update_cost_per_drawer(
    payload: CostSettingsUpdate,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> CostSettingsOut:
    before = float(settings_service.get_cost_per_drawer(db))
    updated = settings_service.set_cost_per_drawer(
        db, decimal.Decimal(str(payload.cost_per_drawer))
    )
    audit_service.record(
        db,
        actor_role=actor_role,
        action="update",
        entity_type="AppSetting",
        entity_id="cost_per_drawer",
        inputs=payload.model_dump(),
        before={"cost_per_drawer": before},
        after={"cost_per_drawer": float(updated)},
    )
    return CostSettingsOut(cost_per_drawer=float(updated))
