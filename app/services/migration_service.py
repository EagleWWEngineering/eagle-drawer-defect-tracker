"""One-time real-data migration: local dev SQLite -> live Render SQLite.

TEMPORARY MODULE - slated for removal (along with app/routers/admin.py and
scripts/export_real_data.py) once Rodolfo has manually confirmed the real
migration against the live Render instance succeeded. See app/routers/admin.py
for the HTTP endpoint that calls import_bundle() below, and
scripts/export_real_data.py for the local script that calls export_bundle().

Why natural keys instead of raw IDs
------------------------------------
Station, DefectCategory, and CustomerIssueCategory are seeded independently in
every database via app/seed_data.py seed_master_data() - their integer primary
keys are NOT guaranteed to match between the source (local dev) and target
(Render) databases, even though the same names exist on both sides. Every
foreign key into one of those three tables is therefore exported by the
referenced row's `name` (never its id) and re-resolved to the target
database's own id for that name at import time. A name that doesn't exist on
the target is a hard, per-record error (see _RecordError) - never a silent
skip or a guess - because master data should already be identical by name on
both sides, so a mismatch indicates something worth investigating.

DefectCase.case_number and CustomerIssue.issue_number are already unique,
stable, human-meaningful identifiers (not synthetic sequence ids), so they
double as the natural key used to (a) decide whether a record already exists
on the target (idempotent create-or-update) and (b) resolve
CustomerIssue.linked_defect_case_id (exported as the linked case's
case_number, re-resolved to the target's own DefectCase.id after defect cases
have been imported - see import_bundle()'s ordering).

Deliberately NOT exported/imported: Station/DefectCategory/CustomerIssueCategory
(master data - already identical by name on both sides via seeding) and
AuditLog/SyncLog (operational logs tied to actions taken in a specific
environment - merging local-dev's audit trail into production's would be
misleading).
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    CustomerIssue,
    CustomerIssueCategory,
    DailyProductionSummary,
    DefectCase,
    DefectCategory,
    DefectItem,
    DefectPhoto,
    Station,
    StatusHistory,
)

EXPORT_VERSION = 1


class _RecordError(Exception):
    """Raised for a single record's import failure - caught per-record by
    import_bundle() so one bad record never aborts the rest of the import."""


# ---------------------------------------------------------------------------
# Small serialization helpers (all fields round-trip through plain JSON types)
# ---------------------------------------------------------------------------


def _dt_to_iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _date_to_iso(value: dt.date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _decimal_to_str(value: decimal.Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _parse_dt(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    return dt.datetime.fromisoformat(value)


def _parse_date(value: Any) -> dt.date | None:
    if value is None:
        return None
    return dt.date.fromisoformat(value)


def _parse_decimal(value: Any) -> decimal.Decimal | None:
    if value is None:
        return None
    return decimal.Decimal(str(value))


def _require(data: dict, key: str):
    if data.get(key) is None:
        raise _RecordError(f"Missing required field '{key}'.")
    return data[key]


def _lookup_id_by_name(
    db: Session, model: type, name: str, cache: dict[str, int], *, label: str
) -> int:
    if name in cache:
        return cache[name]
    row = db.query(model).filter(model.name == name).first()
    if row is None:
        raise _RecordError(
            f"{label} '{name}' does not exist on the target database by name - "
            "master data should already match by name on both sides (same "
            "seed_master_data()); this needs investigating, not guessing."
        )
    cache[name] = row.id
    return row.id


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _export_defect_case(
    case: DefectCase, uploads_dir: Path, missing_photo_files: list[str]
) -> dict:
    items = [
        {
            "defect_category_name": item.defect_category.name,
            "affected_drawer_quantity": item.affected_drawer_quantity,
            "notes": item.notes,
        }
        for item in case.items
    ]

    photos = []
    for photo in case.photos:
        file_path = uploads_dir / photo.stored_filename
        file_missing = not file_path.is_file()
        file_base64 = None
        if file_missing:
            missing_photo_files.append(photo.stored_filename)
        else:
            file_base64 = base64.b64encode(file_path.read_bytes()).decode("ascii")
        photos.append(
            {
                "stored_filename": photo.stored_filename,
                "original_filename": photo.original_filename,
                "content_type": photo.content_type,
                "uploaded_at": _dt_to_iso(photo.uploaded_at),
                "file_missing": file_missing,
                "file_base64": file_base64,
            }
        )

    status_history = [
        {
            "from_status": h.from_status,
            "to_status": h.to_status,
            "note": h.note,
            "changed_at": _dt_to_iso(h.changed_at),
        }
        for h in case.status_history
    ]

    return {
        "case_number": case.case_number,
        "production_date": _date_to_iso(case.production_date),
        "detected_at": _dt_to_iso(case.detected_at),
        "work_order_number": case.work_order_number,
        "drawer_part_reference": case.drawer_part_reference,
        "found_station_name": case.found_station.name,
        "possible_source_station_name": (
            case.possible_source_station.name if case.possible_source_station else None
        ),
        "priority": case.priority,
        "status": case.status,
        "disposition": case.disposition,
        "repair_action": case.repair_action,
        "root_cause": case.root_cause,
        "corrective_action": case.corrective_action,
        "notes": case.notes,
        "resolved_on_the_spot": case.resolved_on_the_spot,
        "skipped_recheck": case.skipped_recheck,
        "created_at": _dt_to_iso(case.created_at),
        "updated_at": _dt_to_iso(case.updated_at),
        "closed_at": _dt_to_iso(case.closed_at),
        "is_deleted": case.is_deleted,
        "items": items,
        "photos": photos,
        "status_history": status_history,
    }


def _export_customer_issue(issue: CustomerIssue) -> dict:
    return {
        "issue_number": issue.issue_number,
        "source_thread_id": issue.source_thread_id,
        "reported_date": _date_to_iso(issue.reported_date),
        "customer_name": issue.customer_name,
        "order_number": issue.order_number,
        "issue_category_name": issue.issue_category.name,
        "source_type": issue.source_type,
        "should_have_caught_at": issue.should_have_caught_at,
        "piece_count": issue.piece_count,
        "estimated_rework_cost": _decimal_to_str(issue.estimated_rework_cost),
        "description": issue.description,
        "photo_urls": issue.photo_urls,
        "status": issue.status,
        "linked_defect_case_number": (
            issue.linked_defect_case.case_number if issue.linked_defect_case else None
        ),
        "notes": issue.notes,
        "created_at": _dt_to_iso(issue.created_at),
        "updated_at": _dt_to_iso(issue.updated_at),
        "is_deleted": issue.is_deleted,
    }


def _export_daily_summary(row: DailyProductionSummary) -> dict:
    return {
        "production_date": _date_to_iso(row.production_date),
        "shift": row.shift,
        "drawers_inspected": row.drawers_inspected,
        "drawers_rejected_unique": row.drawers_rejected_unique,
        "drawers_reworked": row.drawers_reworked,
        "drawers_scrapped": row.drawers_scrapped,
        "notes": row.notes,
        "cost_per_drawer_at_time": _decimal_to_str(row.cost_per_drawer_at_time),
        "created_at": _dt_to_iso(row.created_at),
        "updated_at": _dt_to_iso(row.updated_at),
    }


def export_bundle(db: Session, uploads_dir: Path) -> dict:
    """Build the full JSON-serializable export bundle from `db`.

    Does NOT touch Station/DefectCategory/CustomerIssueCategory (master data -
    identical by name on both sides via seed_master_data()) or AuditLog/SyncLog
    (environment-specific operational logs, deliberately excluded).
    """
    missing_photo_files: list[str] = []

    cases = db.query(DefectCase).order_by(DefectCase.id).all()
    defect_cases = [_export_defect_case(c, uploads_dir, missing_photo_files) for c in cases]

    issues = db.query(CustomerIssue).order_by(CustomerIssue.id).all()
    customer_issues = [_export_customer_issue(i) for i in issues]

    summaries = db.query(DailyProductionSummary).order_by(DailyProductionSummary.id).all()
    daily_production_summaries = [_export_daily_summary(s) for s in summaries]

    return {
        "export_version": EXPORT_VERSION,
        "exported_at": _dt_to_iso(dt.datetime.now(dt.timezone.utc)),
        "defect_cases": defect_cases,
        "customer_issues": customer_issues,
        "daily_production_summaries": daily_production_summaries,
        "missing_photo_files": missing_photo_files,
    }


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _import_defect_case(
    db: Session,
    uploads_dir: Path,
    data: dict,
    station_cache: dict[str, int],
    category_cache: dict[str, int],
) -> tuple[int, bool]:
    case_number = _require(data, "case_number")
    found_station_name = _require(data, "found_station_name")
    found_station_id = _lookup_id_by_name(
        db, Station, found_station_name, station_cache, label="Station"
    )

    possible_source_station_id = None
    possible_source_station_name = data.get("possible_source_station_name")
    if possible_source_station_name:
        possible_source_station_id = _lookup_id_by_name(
            db, Station, possible_source_station_name, station_cache, label="Station"
        )

    existing = db.query(DefectCase).filter(DefectCase.case_number == case_number).first()
    is_update = existing is not None
    case = existing or DefectCase(case_number=case_number)

    case.production_date = _parse_date(_require(data, "production_date"))
    case.detected_at = _parse_dt(_require(data, "detected_at"))
    case.work_order_number = _require(data, "work_order_number")
    case.drawer_part_reference = data.get("drawer_part_reference")
    case.found_station_id = found_station_id
    case.possible_source_station_id = possible_source_station_id
    case.priority = _require(data, "priority")
    case.status = _require(data, "status")
    case.disposition = data.get("disposition")
    case.repair_action = data.get("repair_action")
    case.root_cause = data.get("root_cause")
    case.corrective_action = data.get("corrective_action")
    case.notes = data.get("notes")
    case.resolved_on_the_spot = bool(data.get("resolved_on_the_spot", False))
    case.skipped_recheck = bool(data.get("skipped_recheck", False))
    case.created_at = _parse_dt(_require(data, "created_at"))
    case.updated_at = _parse_dt(_require(data, "updated_at"))
    case.closed_at = _parse_dt(data.get("closed_at"))
    case.is_deleted = bool(data.get("is_deleted", False))

    if not is_update:
        db.add(case)
    db.flush()

    # Replace children wholesale to match the export - same spirit as
    # defect_service.py updating a case in place on reinspection rather than
    # duplicating (this is a full-record sync, not an incremental merge).
    db.query(DefectItem).filter(DefectItem.defect_case_id == case.id).delete(
        synchronize_session=False
    )
    db.query(DefectPhoto).filter(DefectPhoto.defect_case_id == case.id).delete(
        synchronize_session=False
    )
    db.query(StatusHistory).filter(StatusHistory.defect_case_id == case.id).delete(
        synchronize_session=False
    )
    db.flush()

    for item_data in data.get("items", []):
        category_name = _require(item_data, "defect_category_name")
        category_id = _lookup_id_by_name(
            db, DefectCategory, category_name, category_cache, label="Defect category"
        )
        db.add(
            DefectItem(
                defect_case_id=case.id,
                defect_category_id=category_id,
                affected_drawer_quantity=item_data.get("affected_drawer_quantity", 1),
                notes=item_data.get("notes"),
            )
        )

    for photo_data in data.get("photos", []):
        stored_filename = _require(photo_data, "stored_filename")
        if not photo_data.get("file_missing") and photo_data.get("file_base64"):
            uploads_dir.mkdir(parents=True, exist_ok=True)
            file_bytes = base64.b64decode(photo_data["file_base64"])
            (uploads_dir / stored_filename).write_bytes(file_bytes)
        db.add(
            DefectPhoto(
                defect_case_id=case.id,
                stored_filename=stored_filename,
                original_filename=photo_data.get("original_filename", stored_filename),
                content_type=photo_data.get("content_type", "application/octet-stream"),
                uploaded_at=(
                    _parse_dt(photo_data.get("uploaded_at")) or dt.datetime.now(dt.timezone.utc)
                ),
            )
        )

    for h in data.get("status_history", []):
        db.add(
            StatusHistory(
                defect_case_id=case.id,
                from_status=h.get("from_status"),
                to_status=_require(h, "to_status"),
                note=h.get("note"),
                changed_at=_parse_dt(h.get("changed_at")) or dt.datetime.now(dt.timezone.utc),
            )
        )

    db.flush()
    return case.id, is_update


def _import_customer_issue(
    db: Session,
    data: dict,
    category_cache: dict[str, int],
    defect_case_number_to_id: dict[str, int],
) -> tuple[int, bool]:
    issue_number = _require(data, "issue_number")
    category_name = _require(data, "issue_category_name")
    category_id = _lookup_id_by_name(
        db, CustomerIssueCategory, category_name, category_cache, label="Customer issue category"
    )

    linked_defect_case_id = None
    linked_case_number = data.get("linked_defect_case_number")
    if linked_case_number:
        linked_defect_case_id = defect_case_number_to_id.get(linked_case_number)
        if linked_defect_case_id is None:
            linked_case = (
                db.query(DefectCase).filter(DefectCase.case_number == linked_case_number).first()
            )
            if linked_case is None:
                raise _RecordError(
                    f"Linked defect case '{linked_case_number}' does not exist on the "
                    "target database - defect cases must be imported before the "
                    "customer issues that link to them."
                )
            linked_defect_case_id = linked_case.id

    existing = db.query(CustomerIssue).filter(CustomerIssue.issue_number == issue_number).first()
    is_update = existing is not None
    issue = existing or CustomerIssue(issue_number=issue_number)

    issue.source_thread_id = data.get("source_thread_id")
    issue.reported_date = _parse_date(_require(data, "reported_date"))
    issue.customer_name = _require(data, "customer_name")
    issue.order_number = data.get("order_number")
    issue.issue_category_id = category_id
    issue.source_type = _require(data, "source_type")
    issue.should_have_caught_at = data.get("should_have_caught_at")
    issue.piece_count = data.get("piece_count", 1)
    issue.estimated_rework_cost = _parse_decimal(data.get("estimated_rework_cost"))
    issue.description = _require(data, "description")
    issue.photo_urls = data.get("photo_urls")
    issue.status = _require(data, "status")
    issue.linked_defect_case_id = linked_defect_case_id
    issue.notes = data.get("notes")
    issue.created_at = _parse_dt(_require(data, "created_at"))
    issue.updated_at = _parse_dt(_require(data, "updated_at"))
    issue.is_deleted = bool(data.get("is_deleted", False))

    if not is_update:
        db.add(issue)
    db.flush()
    return issue.id, is_update


def _import_daily_summary(db: Session, data: dict) -> tuple[int, bool]:
    production_date = _parse_date(_require(data, "production_date"))
    shift = data.get("shift") or "Day"

    existing = (
        db.query(DailyProductionSummary)
        .filter(
            DailyProductionSummary.production_date == production_date,
            DailyProductionSummary.shift == shift,
        )
        .first()
    )
    is_update = existing is not None
    row = existing or DailyProductionSummary(production_date=production_date, shift=shift)

    row.drawers_inspected = data.get("drawers_inspected", 0)
    row.drawers_rejected_unique = data.get("drawers_rejected_unique", 0)
    row.drawers_reworked = data.get("drawers_reworked", 0)
    row.drawers_scrapped = data.get("drawers_scrapped", 0)
    row.notes = data.get("notes")
    row.cost_per_drawer_at_time = _parse_decimal(data.get("cost_per_drawer_at_time"))
    row.created_at = _parse_dt(_require(data, "created_at"))
    row.updated_at = _parse_dt(_require(data, "updated_at"))

    if not is_update:
        db.add(row)
    db.flush()
    return row.id, is_update


def _empty_table_result() -> dict:
    return {"created": 0, "updated": 0, "skipped": 0, "errors": []}


def _record_success(bucket: dict, is_update: bool) -> None:
    bucket["updated" if is_update else "created"] += 1


def _record_error(bucket: dict, key: str, exc: Exception) -> None:
    bucket["skipped"] += 1
    message = str(exc) if isinstance(exc, _RecordError) else f"Unexpected error: {exc}"
    bucket["errors"].append({"key": key, "message": message})


def import_bundle(db: Session, uploads_dir: Path, bundle: dict) -> dict:
    """Import an export_bundle()-shaped dict into `db`, resolving every master-data
    reference by name against THIS database (never trusting any id in the bundle).

    Idempotent: DefectCase matched by case_number, CustomerIssue by issue_number,
    DailyProductionSummary by (production_date, shift) - a record that already
    exists on the target is updated in place, never duplicated.

    Import order matters: defect cases first, then customer issues (so
    linked_defect_case_number can resolve against defect cases just imported in
    this same call), then daily summaries. Each individual record is imported in
    its own SAVEPOINT (db.begin_nested()) so one bad record (e.g. an unknown
    station name) is rolled back and reported as a per-record error without
    aborting or corrupting the rest of the import.
    """
    station_cache: dict[str, int] = {}
    category_cache: dict[str, int] = {}
    customer_category_cache: dict[str, int] = {}

    result = {
        "defect_cases": _empty_table_result(),
        "customer_issues": _empty_table_result(),
        "daily_production_summaries": _empty_table_result(),
    }

    case_number_to_id: dict[str, int] = {}
    for case_data in bundle.get("defect_cases", []):
        case_number = case_data.get("case_number") or "<missing case_number>"
        try:
            with db.begin_nested():
                case_id, is_update = _import_defect_case(
                    db, uploads_dir, case_data, station_cache, category_cache
                )
            case_number_to_id[case_number] = case_id
            _record_success(result["defect_cases"], is_update)
        except Exception as exc:  # noqa: BLE001 - every failure must become a reported per-record error, never a crash
            _record_error(result["defect_cases"], case_number, exc)
    db.commit()

    for issue_data in bundle.get("customer_issues", []):
        issue_number = issue_data.get("issue_number") or "<missing issue_number>"
        try:
            with db.begin_nested():
                _, is_update = _import_customer_issue(
                    db, issue_data, customer_category_cache, case_number_to_id
                )
            _record_success(result["customer_issues"], is_update)
        except Exception as exc:  # noqa: BLE001
            _record_error(result["customer_issues"], issue_number, exc)
    db.commit()

    for summary_data in bundle.get("daily_production_summaries", []):
        key = f"{summary_data.get('production_date')} / {summary_data.get('shift') or 'Day'}"
        try:
            with db.begin_nested():
                _, is_update = _import_daily_summary(db, summary_data)
            _record_success(result["daily_production_summaries"], is_update)
        except Exception as exc:  # noqa: BLE001
            _record_error(result["daily_production_summaries"], key, exc)
    db.commit()

    return result
