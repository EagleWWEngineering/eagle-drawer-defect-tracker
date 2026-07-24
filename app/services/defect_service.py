"""Core business rules: case numbering, defect-case creation, and status transitions.

This is the ONE place the counting rules (PROJECT_SPEC.md section 2) and the status
transition map (PROJECT_SPEC.md section 3.1) are implemented. Routers call these
functions instead of touching the rules themselves, and the MCP server calls the
REST API (which calls these same functions) so the UI and MCP path can never drift
apart.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.errors import InvalidTransitionError, NotFoundError, ValidationError
from app.models import (
    DailyProductionSummary,
    DefectCase,
    DefectCategory,
    DefectItem,
    Station,
    StatusHistory,
)

VALID_PRIORITIES: list[str] = ["Urgent", "High", "Normal"]

VALID_STATUSES: list[str] = [
    "Open",
    "In Rework",
    "Waiting",
    "Ready for QC Recheck",
    "Closed - Repaired",
    "Closed - Scrapped",
    "Closed - Use As Is",
]

VALID_DISPOSITIONS: list[str] = ["Rework", "Scrap", "Use As Is", "Hold"]

CLOSED_STATUSES: set[str] = {"Closed - Repaired", "Closed - Scrapped", "Closed - Use As Is"}

# The allowed-transition map from PROJECT_SPEC.md section 3.1. Kept as one table so
# the pilot can adjust it without touching any router or template code. Reopening a
# closed case back to Open is handled as a special case (see reopen_case) because it
# always requires a note, unlike every other transition here.
STATUS_TRANSITIONS: dict[str, set[str]] = {
    "Open": {"In Rework", "Waiting", "Closed - Scrapped", "Closed - Use As Is"},
    "In Rework": {"Ready for QC Recheck", "Waiting"},
    "Waiting": {"In Rework", "Closed - Scrapped"},
    "Ready for QC Recheck": {
        "Closed - Repaired",
        "In Rework",
        "Closed - Scrapped",
        "Closed - Use As Is",
    },
    "Closed - Repaired": set(),
    "Closed - Scrapped": set(),
    "Closed - Use As Is": set(),
}

# PROJECT_SPEC.md section 3.2: choosing a disposition implies a target status.
DISPOSITION_TO_STATUS: dict[str, str] = {
    "Rework": "In Rework",
    "Hold": "Waiting",
    "Scrap": "Closed - Scrapped",
    "Use As Is": "Closed - Use As Is",
}


def allowed_next_statuses(current_status: str) -> set[str]:
    return STATUS_TRANSITIONS.get(current_status, set())


def _get_active_or_any_station(db: Session, station_id: int, *, field: str) -> Station:
    station = db.get(Station, station_id)
    if station is None:
        raise NotFoundError(f"Station {station_id} does not exist.", field=field)
    return station


def _get_category(db: Session, category_id: int) -> DefectCategory:
    category = db.get(DefectCategory, category_id)
    if category is None:
        raise NotFoundError(f"Defect category {category_id} does not exist.", field="items")
    return category


def generate_case_number(db: Session, production_date: dt.date) -> str:
    """DF-YYYYMMDD-NNNN with a 4-digit sequence that resets per production_date.

    Assumption: the sequence resets on production_date (the date the paper log/entry
    is attributed to), not the calendar date the record happens to be typed in on —
    this keeps the date embedded in the case number consistent with the date shown
    everywhere else for that case.
    """
    count = (
        db.query(func.count(DefectCase.id))
        .filter(DefectCase.production_date == production_date)
        .scalar()
        or 0
    )
    sequence = count + 1
    return f"DF-{production_date.strftime('%Y%m%d')}-{sequence:04d}"


def _merge_duplicate_items(
    items: list[dict],
) -> dict[int, dict]:
    """Merge items that share a defect_category_id within one submission.

    Rule 3/6: multiple entries for the same category on the same case must never
    double count as separate defect events, so quantities are summed and notes are
    concatenated instead of creating two DefectItem rows (which the DB's unique
    constraint on (case, category) would reject anyway).
    """
    merged: dict[int, dict] = {}
    for item in items:
        category_id = item["defect_category_id"]
        quantity = item.get("affected_drawer_quantity") or 1
        notes = item.get("notes")
        if category_id in merged:
            merged[category_id]["affected_drawer_quantity"] += quantity
            if notes:
                existing_notes = merged[category_id]["notes"]
                merged[category_id]["notes"] = (
                    f"{existing_notes}; {notes}" if existing_notes else notes
                )
        else:
            merged[category_id] = {
                "defect_category_id": category_id,
                "affected_drawer_quantity": quantity,
                "notes": notes,
            }
    return merged


def create_defect_case(
    db: Session,
    *,
    production_date: dt.date,
    detected_at: dt.datetime,
    work_order_number: str,
    drawer_part_reference: str | None,
    found_station_id: int,
    possible_source_station_id: int | None,
    priority: str,
    items: list[dict],
    disposition: str | None = None,
    repair_action: str | None = None,
    root_cause: str | None = None,
    corrective_action: str | None = None,
    notes: str | None = None,
) -> DefectCase:
    if not work_order_number or not work_order_number.strip():
        raise ValidationError("Work order number is required.", field="work_order_number")
    if priority not in VALID_PRIORITIES:
        raise ValidationError(f"Priority must be one of {VALID_PRIORITIES}.", field="priority")
    if disposition is not None and disposition not in VALID_DISPOSITIONS:
        raise ValidationError(
            f"Disposition must be one of {VALID_DISPOSITIONS}.", field="disposition"
        )
    if not items:
        raise ValidationError("At least one defect category is required.", field="items")

    _get_active_or_any_station(db, found_station_id, field="found_station_id")
    if possible_source_station_id is not None:
        _get_active_or_any_station(
            db, possible_source_station_id, field="possible_source_station_id"
        )

    merged_items = _merge_duplicate_items(items)
    for category_id in merged_items:
        _get_category(db, category_id)
        if merged_items[category_id]["affected_drawer_quantity"] < 1:
            raise ValidationError("Affected drawer quantity must be at least 1.", field="items")

    initial_status = DISPOSITION_TO_STATUS.get(disposition, "Open") if disposition else "Open"

    case = DefectCase(
        case_number=generate_case_number(db, production_date),
        production_date=production_date,
        detected_at=detected_at,
        work_order_number=work_order_number.strip(),
        drawer_part_reference=(drawer_part_reference or None),
        found_station_id=found_station_id,
        possible_source_station_id=possible_source_station_id,
        priority=priority,
        status=initial_status,
        disposition=disposition,
        repair_action=repair_action,
        root_cause=root_cause,
        corrective_action=corrective_action,
        notes=notes,
        closed_at=dt.datetime.now(dt.timezone.utc) if initial_status in CLOSED_STATUSES else None,
    )
    case.items = [
        DefectItem(
            defect_category_id=data["defect_category_id"],
            affected_drawer_quantity=data["affected_drawer_quantity"],
            notes=data["notes"],
        )
        for data in merged_items.values()
    ]
    case.status_history = [
        StatusHistory(from_status=None, to_status=initial_status, note="Case created")
    ]

    db.add(case)
    db.commit()
    db.refresh(case)
    return case


def get_case_or_404(db: Session, case_id: int, *, include_deleted: bool = False) -> DefectCase:
    case = db.get(DefectCase, case_id)
    if case is None or (case.is_deleted and not include_deleted):
        raise NotFoundError(f"Defect case {case_id} not found.")
    return case


def get_case_by_number_or_404(db: Session, case_number: str) -> DefectCase:
    case = db.query(DefectCase).filter(DefectCase.case_number == case_number).first()
    if case is None or case.is_deleted:
        raise NotFoundError(f"Defect case {case_number} not found.")
    return case


def add_or_merge_item(
    db: Session,
    case: DefectCase,
    *,
    defect_category_id: int,
    affected_drawer_quantity: int = 1,
    notes: str | None = None,
) -> DefectItem:
    """Add a category to an existing case, merging into an existing item if present.

    This is how rule 6 (reinspection of an unresolved defect updates the existing
    case rather than creating a duplicate) is implemented for the "add a newly
    confirmed category to an existing case" scenario.
    """
    if case.is_deleted:
        raise ValidationError("Cannot modify a deleted case.")
    if affected_drawer_quantity < 1:
        raise ValidationError("Affected drawer quantity must be at least 1.", field="items")
    _get_category(db, defect_category_id)

    existing = next((i for i in case.items if i.defect_category_id == defect_category_id), None)
    if existing is not None:
        existing.affected_drawer_quantity += affected_drawer_quantity
        if notes:
            existing.notes = f"{existing.notes}; {notes}" if existing.notes else notes
        db.commit()
        db.refresh(existing)
        return existing

    item = DefectItem(
        defect_case_id=case.id,
        defect_category_id=defect_category_id,
        affected_drawer_quantity=affected_drawer_quantity,
        notes=notes,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_case_status(
    db: Session,
    case: DefectCase,
    *,
    new_status: str,
    disposition: str | None = None,
    repair_action: str | None = None,
    note: str | None = None,
) -> DefectCase:
    if new_status not in VALID_STATUSES:
        raise ValidationError(f"Status must be one of {VALID_STATUSES}.", field="new_status")
    if disposition is not None and disposition not in VALID_DISPOSITIONS:
        raise ValidationError(
            f"Disposition must be one of {VALID_DISPOSITIONS}.", field="disposition"
        )

    current_status = case.status
    is_reopen = current_status in CLOSED_STATUSES and new_status == "Open"

    if is_reopen:
        if not note or not note.strip():
            raise ValidationError(
                "Reopening a closed case requires a note explaining why.", field="note"
            )
    else:
        allowed = allowed_next_statuses(current_status)
        if new_status not in allowed:
            raise InvalidTransitionError(
                f"Cannot move from '{current_status}' to '{new_status}'. "
                f"Allowed next statuses: {sorted(allowed) or 'none (terminal status)'}.",
                field="new_status",
            )

    case.status = new_status
    if disposition is not None:
        case.disposition = disposition
    if repair_action is not None:
        case.repair_action = repair_action
    case.closed_at = dt.datetime.now(dt.timezone.utc) if new_status in CLOSED_STATUSES else None

    db.add(
        StatusHistory(
            defect_case_id=case.id,
            from_status=current_status,
            to_status=new_status,
            note=note,
        )
    )
    db.commit()
    db.refresh(case)
    return case


def soft_delete_case(db: Session, case: DefectCase) -> DefectCase:
    case.is_deleted = True
    db.commit()
    db.refresh(case)
    return case


def check_daily_summary_warnings(
    *,
    drawers_inspected: int,
    drawers_rejected_unique: int,
    drawers_reworked: int,
    drawers_scrapped: int,
) -> list[str]:
    """Soft warnings from PROJECT_SPEC.md section 2.3 — overridable with a note.

    Rework/scrap commonly land on a later date than the original rejection, so a
    single day can legitimately show rework/scrap with zero (or fewer) rejections
    that same day. These are not hard blocks until the pilot confirms date
    attribution across a multi-day repair cycle.
    """
    warnings: list[str] = []
    if drawers_reworked > drawers_rejected_unique:
        warnings.append("Drawers reworked exceeds unique drawers rejected for this date.")
    if drawers_scrapped > drawers_rejected_unique:
        warnings.append("Drawers scrapped exceeds unique drawers rejected for this date.")
    if drawers_reworked + drawers_scrapped > drawers_rejected_unique:
        warnings.append(
            "Drawers reworked plus scrapped exceeds unique drawers rejected for this date."
        )
    return warnings


def validate_daily_summary_input(
    *,
    drawers_inspected: int,
    drawers_rejected_unique: int,
    drawers_reworked: int,
    drawers_scrapped: int,
    notes: str | None,
) -> list[str]:
    """Hard rule + soft-warning-with-required-note check. Returns warnings for display.

    Raises ValidationError if the hard rule is broken, or if soft warnings exist but
    no note was provided to explain the override.
    """
    for label, value in (
        ("drawers_inspected", drawers_inspected),
        ("drawers_rejected_unique", drawers_rejected_unique),
        ("drawers_reworked", drawers_reworked),
        ("drawers_scrapped", drawers_scrapped),
    ):
        if value < 0:
            raise ValidationError(f"{label} cannot be negative.", field=label)

    if drawers_rejected_unique > drawers_inspected:
        raise ValidationError(
            "Unique drawers rejected cannot exceed drawers inspected.",
            field="drawers_rejected_unique",
        )

    warnings = check_daily_summary_warnings(
        drawers_inspected=drawers_inspected,
        drawers_rejected_unique=drawers_rejected_unique,
        drawers_reworked=drawers_reworked,
        drawers_scrapped=drawers_scrapped,
    )
    if warnings and not (notes and notes.strip()):
        raise ValidationError(
            "This entry looks unusual (" + " ".join(warnings) + ") "
            "Add a note explaining why, then save again to confirm it's correct.",
            field="notes",
        )
    return warnings


def upsert_daily_summary(
    db: Session,
    *,
    production_date: dt.date,
    shift: str,
    drawers_inspected: int,
    drawers_rejected_unique: int,
    drawers_reworked: int,
    drawers_scrapped: int,
    notes: str | None,
) -> tuple[DailyProductionSummary, list[str]]:
    warnings = validate_daily_summary_input(
        drawers_inspected=drawers_inspected,
        drawers_rejected_unique=drawers_rejected_unique,
        drawers_reworked=drawers_reworked,
        drawers_scrapped=drawers_scrapped,
        notes=notes,
    )

    row = (
        db.query(DailyProductionSummary)
        .filter(
            DailyProductionSummary.production_date == production_date,
            DailyProductionSummary.shift == shift,
        )
        .first()
    )
    if row is None:
        row = DailyProductionSummary(production_date=production_date, shift=shift)
        db.add(row)

    row.drawers_inspected = drawers_inspected
    row.drawers_rejected_unique = drawers_rejected_unique
    row.drawers_reworked = drawers_reworked
    row.drawers_scrapped = drawers_scrapped
    row.notes = notes

    db.commit()
    db.refresh(row)
    return row, warnings
