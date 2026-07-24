"""Master data: stations and defect categories (PROJECT_SPEC.md section 3).

Records may be deactivated but are never hard-deleted — there is intentionally no
DELETE route here. Historical defect cases keep referencing them by id.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.dependencies import get_actor_role, get_db
from app.models import DefectCategory, Station
from app.schemas import (
    DefectCategoryCreate,
    DefectCategoryOut,
    DefectCategoryUpdate,
    MasterDataOut,
    StationCreate,
    StationOut,
    StationUpdate,
)
from app.services import audit_service
from app.services.defect_service import VALID_DISPOSITIONS, VALID_PRIORITIES, VALID_STATUSES

router = APIRouter(prefix="/api/v1/master-data", tags=["master-data"])


@router.get("", response_model=MasterDataOut)
def get_master_data(db: Session = Depends(get_db)) -> MasterDataOut:
    stations = db.query(Station).order_by(Station.sort_order, Station.name).all()
    categories = (
        db.query(DefectCategory).order_by(DefectCategory.sort_order, DefectCategory.name).all()
    )
    return MasterDataOut(
        stations=[StationOut.model_validate(s) for s in stations],
        defect_categories=[DefectCategoryOut.model_validate(c) for c in categories],
        priorities=VALID_PRIORITIES,
        statuses=VALID_STATUSES,
        dispositions=VALID_DISPOSITIONS,
    )


@router.post("/stations", response_model=StationOut)
def create_station(
    payload: StationCreate,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> StationOut:
    station = Station(name=payload.name.strip(), sort_order=payload.sort_order, active=True)
    db.add(station)
    db.commit()
    db.refresh(station)
    audit_service.record(
        db,
        actor_role=actor_role,
        action="create",
        entity_type="Station",
        entity_id=station.id,
        inputs=payload.model_dump(),
    )
    return StationOut.model_validate(station)


@router.patch("/stations/{station_id}", response_model=StationOut)
def update_station(
    station_id: int,
    payload: StationUpdate,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> StationOut:
    from app.errors import NotFoundError

    station = db.get(Station, station_id)
    if station is None:
        raise NotFoundError(f"Station {station_id} not found.")

    before = StationOut.model_validate(station).model_dump()
    if payload.name is not None:
        station.name = payload.name.strip()
    if payload.active is not None:
        station.active = payload.active
    if payload.sort_order is not None:
        station.sort_order = payload.sort_order
    db.commit()
    db.refresh(station)

    audit_service.record(
        db,
        actor_role=actor_role,
        action="update",
        entity_type="Station",
        entity_id=station.id,
        inputs=payload.model_dump(exclude_unset=True),
        before=before,
        after=StationOut.model_validate(station).model_dump(),
    )
    return StationOut.model_validate(station)


@router.post("/defect-categories", response_model=DefectCategoryOut)
def create_category(
    payload: DefectCategoryCreate,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> DefectCategoryOut:
    category = DefectCategory(name=payload.name.strip(), sort_order=payload.sort_order, active=True)
    db.add(category)
    db.commit()
    db.refresh(category)
    audit_service.record(
        db,
        actor_role=actor_role,
        action="create",
        entity_type="DefectCategory",
        entity_id=category.id,
        inputs=payload.model_dump(),
    )
    return DefectCategoryOut.model_validate(category)


@router.patch("/defect-categories/{category_id}", response_model=DefectCategoryOut)
def update_category(
    category_id: int,
    payload: DefectCategoryUpdate,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> DefectCategoryOut:
    from app.errors import NotFoundError

    category = db.get(DefectCategory, category_id)
    if category is None:
        raise NotFoundError(f"Defect category {category_id} not found.")

    before = DefectCategoryOut.model_validate(category).model_dump()
    if payload.name is not None:
        category.name = payload.name.strip()
    if payload.active is not None:
        category.active = payload.active
    if payload.sort_order is not None:
        category.sort_order = payload.sort_order
    db.commit()
    db.refresh(category)

    audit_service.record(
        db,
        actor_role=actor_role,
        action="update",
        entity_type="DefectCategory",
        entity_id=category.id,
        inputs=payload.model_dump(exclude_unset=True),
        before=before,
        after=DefectCategoryOut.model_validate(category).model_dump(),
    )
    return DefectCategoryOut.model_validate(category)
