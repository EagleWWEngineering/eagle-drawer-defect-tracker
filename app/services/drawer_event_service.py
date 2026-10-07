"""Drawer scan events pushed by eagle-drawers-production-count
(PROJECT_SPEC_PHASE11.md) - UNDO-card kickbacks and auto-close on re-scan.

POST /api/v1/sync/drawer-events/ingest-raw:

    {"source": "eagle-drawers-production-count",
     "events": [{"event_id": 9123, "type": "kickback", "area": "qc",
                 "order_no": "179459", "order_detail_id": 285016, "unit": 1,
                 "occurred_at": "2026-10-01T14:42:07+00:00"}, ...]}

  - "kickback": the UNDO card took this drawer off a station's count. If the
    drawer has no open case, a Set Aside case is opened for it - category from
    Admin (per area, default Other), found station Assembly or QC / Sorting /
    Shipping, everything else left for someone to fill in later.
  - "counted": the drawer got a +1 at any station. Every open case for it that
    was logged before this scan closes as "Closed - Repaired".

Only unique-ID drawer labels reach here (production count can't name one
drawer otherwise), so matching is always by (order_detail_id, unit).

Each event is stored in drawer_events, unique per (source, event_id): a resend
is counted as a duplicate and changes nothing. An event this app can't act on
(e.g. no station to file it under) is still stored, with the reason as its
outcome, so it isn't retried forever. Any malformed event rejects the whole
request (422) with nothing written.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from sqlalchemy.orm import Session

from app.errors import ServiceError, UnprocessableError
from app.models import DefectCase, DefectCategory, DrawerEvent, Station, SyncLog
from app.services import audit_service, defect_service, order_line_service, settings_service
from app.timezone_utils import to_display_string, today_in_display_timezone

DEFAULT_SOURCE = "eagle-drawers-production-count"
EVENT_TYPES = ("kickback", "counted")
AREAS = ("qc", "assembly")
MAX_EVENTS = 1000
ORDER_NO_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")

# Who did it, in the audit log and the case history.
ACTOR_ROLE = "production-count"
AREA_LABELS = {"qc": "QC", "assembly": "Assembly In"}
# The seeded station each area files its kickbacks under. Looked up by
# seed_key first, so an Admin rename doesn't break it.
AREA_STATIONS = {"qc": "QC / Sorting / Shipping", "assembly": "Assembly"}
FALLBACK_CATEGORY = "Other"

ENTRY_SOURCE = "undo_card"
CLOSE_STATUS = "Closed - Repaired"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _short_time(value: dt.datetime) -> str:
    """'10:42 AM' in the shop's timezone, for case notes."""
    full = to_display_string(value) or ""
    return " ".join(full.split(" ")[1:3])


def validate_payload(data: Any) -> list[dict]:
    """Returns clean event dicts or raises UnprocessableError."""
    if not isinstance(data, dict):
        raise UnprocessableError("Body must be a JSON object.")
    events = data.get("events")
    if not isinstance(events, list):
        raise UnprocessableError("'events' must be a list.", field="events")
    if len(events) > MAX_EVENTS:
        raise UnprocessableError(f"At most {MAX_EVENTS} events per request.", field="events")
    source = data.get("source", DEFAULT_SOURCE)
    if not isinstance(source, str) or not source.strip() or len(source) > 64:
        raise UnprocessableError("'source' must be a short name.", field="source")

    parsed: list[dict] = []
    for i, raw in enumerate(events):
        where = f"events[{i}]"
        if not isinstance(raw, dict):
            raise UnprocessableError(f"{where} must be an object.", field="events")
        event_id = raw.get("event_id")
        if not _is_int(event_id) or event_id <= 0:
            raise UnprocessableError(
                f"{where}: event_id must be a positive integer.", field="events"
            )
        if raw.get("type") not in EVENT_TYPES:
            raise UnprocessableError(f"{where}: type must be one of {EVENT_TYPES}.", field="events")
        if raw.get("area") not in AREAS:
            raise UnprocessableError(f"{where}: area must be one of {AREAS}.", field="events")
        order_no = raw.get("order_no")
        if not isinstance(order_no, str) or not ORDER_NO_RE.match(order_no):
            raise UnprocessableError(f"{where}: order_no is not an order number.", field="events")
        for key in ("order_detail_id", "unit"):
            if not _is_int(raw.get(key)) or raw[key] <= 0:
                raise UnprocessableError(
                    f"{where}: {key} must be a positive integer.", field="events"
                )
        occurred_raw = raw.get("occurred_at")
        try:
            occurred_at = dt.datetime.fromisoformat(occurred_raw)
        except (TypeError, ValueError):
            occurred_at = None
        if occurred_at is None or occurred_at.tzinfo is None:
            raise UnprocessableError(
                f"{where}: occurred_at must be an ISO datetime with a timezone.", field="events"
            )
        parsed.append(
            {
                "source": source.strip(),
                "event_id": event_id,
                "type": raw["type"],
                "area": raw["area"],
                "order_no": order_no,
                "order_detail_id": raw["order_detail_id"],
                "unit": raw["unit"],
                "occurred_at": occurred_at.astimezone(dt.timezone.utc),
                "clamp": _clamp(raw.get("clamp")),
            }
        )
    return parsed


def _clamp(value) -> str | None:
    """Optional (2026-10 redesign); anything that isn't short text is ignored
    rather than refusing the whole batch."""
    if isinstance(value, str) and value.strip():
        return value.strip()[:40]
    return None


def _seeded(db: Session, model: type[Station] | type[DefectCategory], default_name: str):
    """The row for a built-in name. Live (active, not deleted) rows first: since the
    09-03 duplicate incident some built-in names sit on hidden leftover rows (e.g.
    "Area 3" holds "QC / Sorting / Shipping") while the real one was renamed."""
    live = (model.active.is_(True)) & (model.is_deleted.is_(False))
    return (
        db.query(model).filter(model.seed_key == default_name, live).first()
        or db.query(model).filter(model.name == default_name, live).first()
        or db.query(model).filter(model.seed_key == default_name).first()
        or db.query(model).filter(model.name == default_name).first()
    )


def kickback_station(db: Session, area: str) -> Station | None:
    """Admin's choice for this area (Admin > UNDO Card), else the built-in station
    by name - live rows first."""
    station_id = settings_service.get_undo_station_id(db, area)
    if station_id is not None:
        station = db.get(Station, station_id)
        if station is not None and not station.is_deleted:
            return station
    return _seeded(db, Station, AREA_STATIONS[area])


def kickback_category(db: Session, area: str) -> DefectCategory | None:
    """Admin's choice for this area, else the built-in Other category."""
    category_id = settings_service.get_undo_category_id(db, area)
    if category_id is not None:
        category = db.get(DefectCategory, category_id)
        if category is not None:
            return category
    return _seeded(db, DefectCategory, FALLBACK_CATEGORY)


def open_cases_for_drawer(db: Session, order_detail_id: int, unit: int) -> list[DefectCase]:
    return (
        db.query(DefectCase)
        .filter(
            DefectCase.order_detail_id == order_detail_id,
            DefectCase.drawer_unit == unit,
            DefectCase.is_deleted.is_(False),
            DefectCase.status.in_(defect_service.DIRECT_CLOSE_SOURCE_STATUSES),
        )
        .order_by(DefectCase.detected_at)
        .all()
    )


def _line_letter(db: Session, event: dict) -> str | None:
    """The line letter from the order-lines feed - only when that line is on
    the same order (same trust rule as label_service.resolve_label)."""
    row = order_line_service.get_line(db, event["order_detail_id"])
    return row.line if row is not None and row.order_no == event["order_no"] else None


def _apply_kickback(db: Session, event: dict) -> tuple[str, int | None, bool]:
    existing = open_cases_for_drawer(db, event["order_detail_id"], event["unit"])
    if existing:
        return f"already open: {existing[0].case_number}", existing[0].id, False

    station = kickback_station(db, event["area"])
    if station is None:
        return f"failed: no '{AREA_STATIONS[event['area']]}' station", None, False
    category = kickback_category(db, event["area"])
    if category is None:
        return "failed: no kickback category set and no 'Other' category", None, False

    label = AREA_LABELS[event["area"]]
    occurred = event["occurred_at"]
    case = defect_service.create_defect_case(
        db,
        production_date=today_in_display_timezone(occurred),
        detected_at=occurred,
        work_order_number=event["order_no"],
        drawer_part_reference=None,
        found_station_id=station.id,
        possible_source_station_id=None,
        priority="Normal",
        items=[{"defect_category_id": category.id, "affected_drawer_quantity": 1, "notes": None}],
        disposition="Set Aside",
        line_label=_line_letter(db, event),
        entry_source=ENTRY_SOURCE,
        order_detail_id=event["order_detail_id"],
        drawer_unit=event["unit"],
        notes=(
            f"Kicked back with the UNDO card at {label}, {_short_time(occurred)}. "
            "Problem not described yet."
        ),
    )
    audit_service.record(
        db,
        actor_role=ACTOR_ROLE,
        action="create",
        entity_type="DefectCase",
        entity_id=case.case_number,
        inputs={k: str(v) for k, v in event.items()},
        after={"case_number": case.case_number, "status": case.status},
    )
    return f"case created: {case.case_number}", case.id, True


def _apply_counted(db: Session, event: dict) -> tuple[str, int | None, int]:
    occurred = event["occurred_at"]
    to_close = [
        case
        for case in open_cases_for_drawer(db, event["order_detail_id"], event["unit"])
        if _as_utc(case.detected_at) < occurred
    ]
    if not to_close:
        return "no open case", None, 0
    note = f"Auto-closed: scanned at {AREA_LABELS[event['area']]} {_short_time(occurred)}"
    for case in to_close:
        before = case.status
        defect_service.update_case_status(db, case, new_status=CLOSE_STATUS, note=note)
        audit_service.record(
            db,
            actor_role=ACTOR_ROLE,
            action="status_change",
            entity_type="DefectCase",
            entity_id=case.case_number,
            inputs={k: str(v) for k, v in event.items()},
            before={"status": before},
            after={"status": case.status},
        )
    numbers = ", ".join(case.case_number for case in to_close)
    return f"closed: {numbers}", to_close[0].id, len(to_close)


def process_payload(db: Session, data: Any) -> dict[str, int]:
    """Validate, apply each new event in the order it happened, store it.
    Returns {received, duplicates, cases_created, cases_closed, failed}."""
    events = validate_payload(data)
    started = dt.datetime.now(dt.timezone.utc)
    duplicates = created = closed = failed = 0

    for event in sorted(events, key=lambda e: (e["occurred_at"], e["event_id"])):
        seen = (
            db.query(DrawerEvent.id)
            .filter(
                DrawerEvent.source == event["source"], DrawerEvent.event_id == event["event_id"]
            )
            .first()
        )
        if seen is not None:
            duplicates += 1
            continue

        try:
            if event["type"] == "kickback":
                outcome, case_id, made = _apply_kickback(db, event)
                created += int(made)
            else:
                outcome, case_id, n_closed = _apply_counted(db, event)
                closed += n_closed
        except ServiceError as err:
            db.rollback()
            outcome, case_id = f"failed: {err}", None
        if outcome.startswith("failed"):
            failed += 1

        db.add(
            DrawerEvent(
                source=event["source"],
                event_id=event["event_id"],
                event_type=event["type"],
                area=event["area"],
                order_no=event["order_no"],
                order_detail_id=event["order_detail_id"],
                drawer_unit=event["unit"],
                occurred_at=event["occurred_at"],
                received_at=dt.datetime.now(dt.timezone.utc),
                outcome=outcome[:255],
                defect_case_id=case_id,
                clamp=event["clamp"],
            )
        )
        db.commit()

    # Most pushes are plain +1 scans that touch no case - only log the ones
    # that did something, so the Admin Sync Log isn't buried in them.
    if created or closed or failed:
        source = events[0]["source"] if events else DEFAULT_SOURCE
        db.add(
            SyncLog(
                sync_started_at=started,
                sync_completed_at=dt.datetime.now(dt.timezone.utc),
                source_url=f"relay:{source}/drawer-events"[:255],
                records_fetched=len(events),
                records_created=created,
                records_updated=closed,
                records_skipped=duplicates,
                errors=f"{failed} event(s) could not be applied" if failed else None,
                status="success" if not failed else "partial",
            )
        )
        db.commit()

    return {
        "received": len(events),
        "duplicates": duplicates,
        "cases_created": created,
        "cases_closed": closed,
        "failed": failed,
    }
