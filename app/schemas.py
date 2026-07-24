"""Pydantic v2 request/response models. Validation shape only — business rules live
in app/services/*.py so the same rules apply no matter which router calls them.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field, computed_field, field_validator

from app.services.defect_service import allowed_next_statuses
from app.timezone_utils import to_display_string

# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


class StationOut(BaseModel):
    id: int
    name: str
    active: bool
    sort_order: int

    model_config = {"from_attributes": True}


class DefectCategoryOut(BaseModel):
    id: int
    name: str
    active: bool
    sort_order: int

    model_config = {"from_attributes": True}


class MasterDataOut(BaseModel):
    stations: list[StationOut]
    defect_categories: list[DefectCategoryOut]
    priorities: list[str]
    statuses: list[str]
    dispositions: list[str]


class StationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sort_order: int = 0


class StationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None
    sort_order: int | None = None


class DefectCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sort_order: int = 0


class DefectCategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None
    sort_order: int | None = None


# ---------------------------------------------------------------------------
# Defect cases
# ---------------------------------------------------------------------------


class DefectItemIn(BaseModel):
    defect_category_id: int
    affected_drawer_quantity: int = Field(default=1, ge=1)
    notes: str | None = None


class DefectItemOut(BaseModel):
    id: int
    defect_category_id: int
    defect_category_name: str
    affected_drawer_quantity: int
    notes: str | None

    model_config = {"from_attributes": True}


class StatusHistoryOut(BaseModel):
    id: int
    from_status: str | None
    to_status: str
    note: str | None
    changed_at: dt.datetime

    @computed_field
    @property
    def changed_at_local(self) -> str | None:
        return to_display_string(self.changed_at)

    model_config = {"from_attributes": True}


class DefectPhotoOut(BaseModel):
    id: int
    original_filename: str
    content_type: str
    uploaded_at: dt.datetime
    stored_filename: str

    @computed_field
    @property
    def url(self) -> str:
        return f"/uploads/{self.stored_filename}"

    model_config = {"from_attributes": True}


class DefectCaseCreate(BaseModel):
    production_date: dt.date
    detected_at: dt.datetime
    work_order_number: str = Field(min_length=1, max_length=60)
    drawer_part_reference: str | None = Field(default=None, max_length=120)
    found_station_id: int
    possible_source_station_id: int | None = None
    priority: str = "Normal"
    items: list[DefectItemIn] = Field(min_length=1)
    disposition: str | None = None
    repair_action: str | None = None
    root_cause: str | None = None
    corrective_action: str | None = None
    notes: str | None = None

    @field_validator("work_order_number")
    @classmethod
    def _strip_wo(cls, v: str) -> str:
        return v.strip()


class DefectCaseUpdate(BaseModel):
    """Partial edit of a case. Adding new items merges into existing ones by category."""

    production_date: dt.date | None = None
    detected_at: dt.datetime | None = None
    work_order_number: str | None = Field(default=None, min_length=1, max_length=60)
    drawer_part_reference: str | None = None
    found_station_id: int | None = None
    possible_source_station_id: int | None = None
    priority: str | None = None
    repair_action: str | None = None
    root_cause: str | None = None
    corrective_action: str | None = None
    notes: str | None = None
    add_items: list[DefectItemIn] | None = None


class DefectCaseStatusChange(BaseModel):
    new_status: str
    disposition: str | None = None
    repair_action: str | None = None
    note: str | None = None


class DefectCaseOut(BaseModel):
    id: int
    case_number: str
    production_date: dt.date
    detected_at: dt.datetime
    work_order_number: str
    drawer_part_reference: str | None
    found_station_id: int
    found_station_name: str
    possible_source_station_id: int | None
    possible_source_station_name: str | None
    priority: str
    status: str
    disposition: str | None
    repair_action: str | None
    root_cause: str | None
    corrective_action: str | None
    notes: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    closed_at: dt.datetime | None
    items: list[DefectItemOut]
    photos: list[DefectPhotoOut]
    status_history: list[StatusHistoryOut]
    defect_event_count: int

    @computed_field
    @property
    def detected_at_local(self) -> str | None:
        return to_display_string(self.detected_at)

    @computed_field
    @property
    def created_at_local(self) -> str | None:
        return to_display_string(self.created_at)

    @computed_field
    @property
    def closed_at_local(self) -> str | None:
        return to_display_string(self.closed_at)

    @computed_field
    @property
    def allowed_next_statuses(self) -> list[str]:
        """What the Rework Queue UI is allowed to offer next, straight from the one
        transition map in app/services/defect_service.py — the UI never re-implements
        this rule itself. Reopening a closed case to Open is always offered here too;
        the service layer still enforces that reopen requires a note."""
        options = sorted(allowed_next_statuses(self.status))
        if self.status in {"Closed - Repaired", "Closed - Scrapped", "Closed - Use As Is"}:
            options = ["Open", *options]
        return options

    model_config = {"from_attributes": True}


def defect_case_to_out(case) -> DefectCaseOut:
    """Build a DefectCaseOut from an ORM DefectCase, filling in derived/display fields."""
    return DefectCaseOut(
        id=case.id,
        case_number=case.case_number,
        production_date=case.production_date,
        detected_at=case.detected_at,
        work_order_number=case.work_order_number,
        drawer_part_reference=case.drawer_part_reference,
        found_station_id=case.found_station_id,
        found_station_name=case.found_station.name,
        possible_source_station_id=case.possible_source_station_id,
        possible_source_station_name=(
            case.possible_source_station.name if case.possible_source_station else None
        ),
        priority=case.priority,
        status=case.status,
        disposition=case.disposition,
        repair_action=case.repair_action,
        root_cause=case.root_cause,
        corrective_action=case.corrective_action,
        notes=case.notes,
        created_at=case.created_at,
        updated_at=case.updated_at,
        closed_at=case.closed_at,
        items=[
            DefectItemOut(
                id=i.id,
                defect_category_id=i.defect_category_id,
                defect_category_name=i.defect_category.name,
                affected_drawer_quantity=i.affected_drawer_quantity,
                notes=i.notes,
            )
            for i in case.items
        ],
        photos=list(case.photos),
        status_history=list(case.status_history),
        defect_event_count=sum(i.affected_drawer_quantity for i in case.items),
    )


class DefectCaseListOut(BaseModel):
    total: int
    cases: list[DefectCaseOut]


# ---------------------------------------------------------------------------
# Daily production summary
# ---------------------------------------------------------------------------


class DailyProductionSummaryIn(BaseModel):
    shift: str = "Day"
    drawers_inspected: int = Field(ge=0)
    drawers_rejected_unique: int = Field(ge=0)
    drawers_reworked: int = Field(ge=0)
    drawers_scrapped: int = Field(ge=0)
    notes: str | None = None


class DailyProductionSummaryOut(BaseModel):
    id: int
    production_date: dt.date
    shift: str
    drawers_inspected: int
    drawers_rejected_unique: int
    drawers_reworked: int
    drawers_scrapped: int
    notes: str | None
    warnings: list[str] = []

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Reports / rework queue
# ---------------------------------------------------------------------------


class KpiOut(BaseModel):
    drawers_inspected: int
    defect_events: int
    unique_drawers_rejected: int
    drawers_reworked: int
    drawers_scrapped: int
    defects_per_100: float | None
    rejection_rate: float | None
    first_pass_yield: float | None
    rework_rate: float | None
    scrap_rate: float | None


class ParetoRowOut(BaseModel):
    label: str
    defect_events: int
    cumulative_pct: float | None


class TrendPointOut(BaseModel):
    period: str
    defect_events: int
    drawers_inspected: int
    unique_drawers_rejected: int


class ReworkQueueItemOut(BaseModel):
    id: int
    case_number: str
    work_order_number: str
    categories: list[str]
    status: str
    disposition: str | None
    priority: str
    found_station_name: str
    possible_source_station_name: str | None
    detected_at: dt.datetime
    age_hours: float

    @computed_field
    @property
    def detected_at_local(self) -> str | None:
        return to_display_string(self.detected_at)

    @computed_field
    @property
    def allowed_next_statuses(self) -> list[str]:
        return sorted(allowed_next_statuses(self.status))


class WorkOrderHistoryOut(BaseModel):
    work_order_number: str
    cases: list[DefectCaseOut]
    total_defect_events: int


# ---------------------------------------------------------------------------
# Generic API envelopes
# ---------------------------------------------------------------------------


class ErrorDetail(BaseModel):
    message: str
    field: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthOut(BaseModel):
    status: str
    database: str
