"""Master data: stations and defect categories (PROJECT_SPEC.md section 3).

Records may be deactivated but are never hard-deleted — there is intentionally no
DELETE route here. Historical defect cases keep referencing them by id.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.config import get_settings
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
from app.services import audit_service, master_data_service
from app.services.defect_service import (
    ALL_KNOWN_DISPOSITIONS,
    ALL_KNOWN_STATUSES,
    VALID_DISPOSITIONS,
    VALID_PRIORITIES,
    VALID_STATUSES,
)

router = APIRouter(prefix="/api/v1/master-data", tags=["master-data"])


@router.get("", response_model=MasterDataOut)
def get_master_data(
    db: Session = Depends(get_db),
    active_only: bool = Query(
        default=False,
        description=(
            "When true, excludes deactivated stations/categories - for entry forms "
            "(e.g. New Defect) where a retired value must never be offered as a new "
            "choice. Leave false for Reports/Dashboard/Admin, which still need "
            "retired values reachable for historical filtering."
        ),
    ),
) -> MasterDataOut:
    station_query = db.query(Station)
    category_query = db.query(DefectCategory)
    if active_only:
        station_query = station_query.filter(Station.active.is_(True))
        category_query = category_query.filter(DefectCategory.active.is_(True))
    stations = station_query.order_by(Station.sort_order, Station.name).all()
    categories = category_query.order_by(DefectCategory.sort_order, DefectCategory.name).all()
    return MasterDataOut(
        stations=[StationOut.model_validate(s) for s in stations],
        defect_categories=[DefectCategoryOut.model_validate(c) for c in categories],
        priorities=VALID_PRIORITIES,
        statuses=VALID_STATUSES,
        dispositions=VALID_DISPOSITIONS,
        all_statuses=ALL_KNOWN_STATUSES,
        all_dispositions=ALL_KNOWN_DISPOSITIONS,
        favorites_enabled=get_settings().favorites_enabled,
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
    existing = db.get(Station, station_id)
    before = StationOut.model_validate(existing).model_dump() if existing else None

    station = master_data_service.update_station(
        db,
        station_id,
        name=payload.name,
        active=payload.active,
        sort_order=payload.sort_order,
        is_favorite=payload.is_favorite,
    )

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
    existing = db.get(DefectCategory, category_id)
    before = DefectCategoryOut.model_validate(existing).model_dump() if existing else None

    category = master_data_service.update_category(
        db,
        category_id,
        name=payload.name,
        active=payload.active,
        sort_order=payload.sort_order,
        is_favorite=payload.is_favorite,
    )

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
