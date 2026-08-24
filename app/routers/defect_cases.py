"""Defect case CRUD, status transitions, and photo uploads.

HTTP input/output only — every business rule is delegated to
app/services/defect_service.py (PROJECT_SPEC.md architecture rule).
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.dependencies import get_actor_role, get_db
from app.errors import ValidationError
from app.models import DefectCase, DefectItem, DefectPhoto
from app.schemas import (
    BulkActionOut,
    BulkIdsIn,
    DefectCaseCreate,
    DefectCaseListOut,
    DefectCaseOut,
    DefectCaseStatusChange,
    DefectCaseUpdate,
    DefectPhotoOut,
    WorkOrderLastStationOut,
    defect_case_to_out,
)
from app.services import audit_service, defect_service

router = APIRouter(prefix="/api/v1/defect-cases", tags=["defect-cases"])

ALLOWED_PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _case_query(db: Session):
    return db.query(DefectCase).options(
        selectinload(DefectCase.items).selectinload(DefectItem.defect_category),
        selectinload(DefectCase.photos),
        selectinload(DefectCase.status_history),
        selectinload(DefectCase.found_station),
        selectinload(DefectCase.possible_source_station),
    )


@router.post("", response_model=DefectCaseOut)
def create_case(
    payload: DefectCaseCreate,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> DefectCaseOut:
    case = defect_service.create_defect_case(
        db,
        production_date=payload.production_date,
        detected_at=payload.detected_at,
        work_order_number=payload.work_order_number,
        drawer_part_reference=payload.drawer_part_reference,
        found_station_id=payload.found_station_id,
        possible_source_station_id=payload.possible_source_station_id,
        priority=payload.priority,
        items=[i.model_dump() for i in payload.items],
        disposition=payload.disposition,
        resolved_on_the_spot=payload.resolved_on_the_spot,
        instant_close_outcome=payload.instant_close_outcome,
        repair_action=payload.repair_action,
        root_cause=payload.root_cause,
        corrective_action=payload.corrective_action,
        notes=payload.notes,
    )
    audit_service.record(
        db,
        actor_role=actor_role,
        action="create",
        entity_type="DefectCase",
        entity_id=case.case_number,
        inputs=payload.model_dump(mode="json"),
        after={"case_number": case.case_number, "status": case.status},
    )
    return defect_case_to_out(case)


@router.get("", response_model=DefectCaseListOut)
def list_cases(
    db: Session = Depends(get_db),
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    work_order_number: str | None = None,
    category_id: int | None = None,
    found_station_id: int | None = None,
    possible_source_station_id: int | None = None,
    priority: str | None = None,
    status: str | None = None,
    disposition: str | None = None,
    include_deleted: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> DefectCaseListOut:
    query = _case_query(db)
    if not include_deleted:
        query = query.filter(DefectCase.is_deleted.is_(False))
    if start_date is not None:
        query = query.filter(DefectCase.production_date >= start_date)
    if end_date is not None:
        query = query.filter(DefectCase.production_date <= end_date)
    if work_order_number:
        query = query.filter(DefectCase.work_order_number.ilike(f"%{work_order_number}%"))
    if found_station_id is not None:
        query = query.filter(DefectCase.found_station_id == found_station_id)
    if possible_source_station_id is not None:
        query = query.filter(DefectCase.possible_source_station_id == possible_source_station_id)
    if priority is not None:
        query = query.filter(DefectCase.priority == priority)
    if status is not None:
        query = query.filter(DefectCase.status == status)
    if disposition is not None:
        query = query.filter(DefectCase.disposition == disposition)
    if category_id is not None:
        query = query.filter(
            DefectCase.id.in_(
                select(DefectItem.defect_case_id).where(
                    DefectItem.defect_category_id == category_id
                )
            )
        )

    total = query.count()
    cases = (
        query.order_by(DefectCase.detected_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return DefectCaseListOut(total=total, cases=[defect_case_to_out(c) for c in cases])


@router.get("/work-orders/recent", response_model=list[str])
def list_recent_work_orders(
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[str]:
    """Most-recently-used work order numbers, for the New Defect form's autocomplete
    (entry-speed fix - see app/templates/defect_entry.html)."""
    return defect_service.list_recent_work_order_numbers(db, limit=limit)


@router.get(
    "/work-orders/{work_order_number}/last-station", response_model=WorkOrderLastStationOut | None
)
def get_last_station_for_work_order(
    work_order_number: str, db: Session = Depends(get_db)
) -> WorkOrderLastStationOut | None:
    """Found Station to pre-fill when the operator re-types a work order that
    already has a case logged against it. Returns null (not a 404) when the work
    order has no prior case - "nothing to pre-fill with" isn't an error."""
    case = defect_service.get_last_case_for_work_order(db, work_order_number)
    if case is None:
        return None
    return WorkOrderLastStationOut(
        work_order_number=case.work_order_number,
        found_station_id=case.found_station_id,
        found_station_name=case.found_station.name,
    )


@router.get("/by-number/{case_number}", response_model=DefectCaseOut)
def get_case_by_number(case_number: str, db: Session = Depends(get_db)) -> DefectCaseOut:
    """Look up a case by its human-readable case number (e.g. DF-20260724-0001).

    Used by the MCP server's get_defect_case / update_defect_case_status tools, which
    only know the case number, not the internal database id.
    """
    case = _case_query(db).filter(DefectCase.case_number == case_number).first()
    if case is None or case.is_deleted:
        from app.errors import NotFoundError

        raise NotFoundError(f"Defect case {case_number} not found.")
    return defect_case_to_out(case)


@router.get("/{case_id}", response_model=DefectCaseOut)
def get_case(case_id: int, db: Session = Depends(get_db)) -> DefectCaseOut:
    case = _case_query(db).filter(DefectCase.id == case_id).first()
    if case is None or case.is_deleted:
        from app.errors import NotFoundError

        raise NotFoundError(f"Defect case {case_id} not found.")
    return defect_case_to_out(case)


@router.patch("/{case_id}", response_model=DefectCaseOut)
def update_case(
    case_id: int,
    payload: DefectCaseUpdate,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> DefectCaseOut:
    case = defect_service.get_case_or_404(db, case_id)
    before = defect_case_to_out(case).model_dump(mode="json")

    updates = payload.model_dump(exclude_unset=True, exclude={"add_items"})
    for field, value in updates.items():
        setattr(case, field, value)
    db.commit()

    if payload.add_items:
        for item in payload.add_items:
            defect_service.add_or_merge_item(
                db,
                case,
                defect_category_id=item.defect_category_id,
                affected_drawer_quantity=item.affected_drawer_quantity,
                notes=item.notes,
            )

    db.refresh(case)
    audit_service.record(
        db,
        actor_role=actor_role,
        action="update",
        entity_type="DefectCase",
        entity_id=case.case_number,
        inputs=payload.model_dump(exclude_unset=True, mode="json"),
        before=before,
        after=defect_case_to_out(case).model_dump(mode="json"),
    )
    return defect_case_to_out(case)


@router.post("/{case_id}/status", response_model=DefectCaseOut)
def change_status(
    case_id: int,
    payload: DefectCaseStatusChange,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> DefectCaseOut:
    case = defect_service.get_case_or_404(db, case_id)
    before_status = case.status
    updated = defect_service.update_case_status(
        db,
        case,
        new_status=payload.new_status,
        disposition=payload.disposition,
        repair_action=payload.repair_action,
        note=payload.note,
    )
    audit_service.record(
        db,
        actor_role=actor_role,
        action="status_change",
        entity_type="DefectCase",
        entity_id=case.case_number,
        inputs=payload.model_dump(),
        before={"status": before_status},
        after={"status": updated.status},
    )
    return defect_case_to_out(updated)


@router.delete("/{case_id}", response_model=DefectCaseOut)
def soft_delete_case(
    case_id: int,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> DefectCaseOut:
    case = defect_service.get_case_or_404(db, case_id)
    deleted = defect_service.soft_delete_case(db, case)
    audit_service.record(
        db,
        actor_role=actor_role,
        action="soft_delete",
        entity_type="DefectCase",
        entity_id=case.case_number,
    )
    return defect_case_to_out(deleted)


@router.post("/bulk-delete", response_model=BulkActionOut)
def bulk_delete_cases(
    payload: BulkIdsIn,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> BulkActionOut:
    cases = defect_service.bulk_soft_delete_cases(db, payload.ids)
    for case in cases:
        audit_service.record(
            db,
            actor_role=actor_role,
            action="soft_delete",
            entity_type="DefectCase",
            entity_id=case.case_number,
        )
    return BulkActionOut(count=len(cases), ids=[c.id for c in cases])


@router.post("/bulk-restore", response_model=BulkActionOut)
def bulk_restore_cases(
    payload: BulkIdsIn,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> BulkActionOut:
    cases = defect_service.bulk_restore_cases(db, payload.ids)
    for case in cases:
        audit_service.record(
            db,
            actor_role=actor_role,
            action="restore",
            entity_type="DefectCase",
            entity_id=case.case_number,
        )
    return BulkActionOut(count=len(cases), ids=[c.id for c in cases])


@router.post("/{case_id}/photos", response_model=DefectPhotoOut)
async def upload_photo(
    case_id: int,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
    file: UploadFile = File(...),
) -> DefectPhotoOut:
    case = defect_service.get_case_or_404(db, case_id)
    settings = get_settings()

    if file.content_type not in ALLOWED_PHOTO_TYPES:
        raise ValidationError(
            f"Unsupported photo type '{file.content_type}'. "
            f"Allowed types: {sorted(ALLOWED_PHOTO_TYPES)}.",
            field="file",
        )

    contents = await file.read()
    if len(contents) > settings.max_upload_bytes:
        raise ValidationError(f"Photo exceeds the {settings.max_upload_mb} MB limit.", field="file")
    if len(contents) == 0:
        raise ValidationError("Uploaded file is empty.", field="file")

    extension = ALLOWED_PHOTO_TYPES[file.content_type]
    safe_original = _SAFE_NAME_RE.sub("_", Path(file.filename or "photo").name)
    stored_filename = f"{case.case_number}_{uuid.uuid4().hex}{extension}"

    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    (settings.uploads_dir / stored_filename).write_bytes(contents)

    photo = DefectPhoto(
        defect_case_id=case.id,
        stored_filename=stored_filename,
        original_filename=safe_original,
        content_type=file.content_type,
    )
    db.add(photo)
    db.commit()
    db.refresh(photo)

    audit_service.record(
        db,
        actor_role=actor_role,
        action="photo_upload",
        entity_type="DefectCase",
        entity_id=case.case_number,
        inputs={"original_filename": safe_original, "content_type": file.content_type},
    )
    return DefectPhotoOut.model_validate(photo)
