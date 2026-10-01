"""App settings: cost_per_drawer (Phase 4) and the UNDO-card kickback
categories (Phase 11). HTTP input/output only -
business rules (validation, persistence) live in app/services/settings_service.py.
"""

from __future__ import annotations

import decimal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.dependencies import get_actor_role, get_db
from app.schemas import CostSettingsOut, CostSettingsUpdate, UndoCategorySettings
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


def _undo_categories(db: Session) -> UndoCategorySettings:
    return UndoCategorySettings(
        qc_category_id=settings_service.get_undo_category_id(db, "qc"),
        assembly_category_id=settings_service.get_undo_category_id(db, "assembly"),
    )


@router.get("/undo-categories", response_model=UndoCategorySettings)
def get_undo_categories(db: Session = Depends(get_db)) -> UndoCategorySettings:
    return _undo_categories(db)


@router.put("/undo-categories", response_model=UndoCategorySettings)
def update_undo_categories(
    payload: UndoCategorySettings,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> UndoCategorySettings:
    before = _undo_categories(db)
    settings_service.set_undo_category_ids(
        db, {"qc": payload.qc_category_id, "assembly": payload.assembly_category_id}
    )
    after = _undo_categories(db)
    audit_service.record(
        db,
        actor_role=actor_role,
        action="update",
        entity_type="AppSetting",
        entity_id="undo_categories",
        inputs=payload.model_dump(),
        before=before.model_dump(),
        after=after.model_dump(),
    )
    return after
