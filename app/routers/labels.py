"""Drawer label lookup for the New Defect form's scanner (PROJECT_SPEC_PHASE10.md
Part 2). HTTP input/output only - parsing and lookup live in
app/services/label_service.py. Behind the normal login like every UI endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.schemas import LabelResolveIn, LabelResolveOut
from app.services import label_service

router = APIRouter(prefix="/api/v1/labels", tags=["labels"])


@router.post("/resolve", response_model=LabelResolveOut)
def resolve_label(payload: LabelResolveIn, db: Session = Depends(get_db)) -> LabelResolveOut:
    """Raw QR text in; order number, drawer identity and (when production count
    has pushed that line) line letter + drawer detail out. Read-only."""
    return LabelResolveOut(**label_service.resolve_label(db, payload.text))
