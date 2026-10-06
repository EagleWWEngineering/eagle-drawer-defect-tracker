"""Work order lines pushed by eagle-drawers-production-count
(PROJECT_SPEC_PHASE10.md Part 2).

A unique-ID drawer label only carries order_detail_id (the Access order-line
record id); turning that into the line letter the New Defect form needs takes
Access data this app (on Render) can't reach. Production count sends a full
snapshot of every open order it has line detail for, hourly, to
POST /api/v1/sync/order-lines/ingest-raw:

    {"source": ..., "generated_at": ...,
     "orders": {"179459": {"lines": [
         {"order_detail_id": 285016, "line": "A", "qty": 2,
          "detail": {"Size": "8 x 37.875 x 27", "Wood": "maple", ...}}]}}}

Each order may also carry "customer" (2026-10 redesign), stored per order in
work_orders. Optional: a payload without it is still valid and leaves any stored
name alone. Cases saved before their lines arrived get their line letter filled
in here (_backfill_case_lines).

Rules:
  - each order in the body has its stored lines REPLACED (lines no longer
    listed for that order are removed);
  - orders absent from the body are left alone - the snapshot is open orders
    only, and a closed/shipped order's lines must survive for defects logged
    after shipping;
  - any invalid entry rejects the whole request (422), nothing written.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.errors import UnprocessableError
from app.models import DefectCase, OrderLine, SyncLog, WorkOrder
from app.services.defect_service import normalize_line_label

ORDER_NO_RE = re.compile(r"^\d{1,20}$")
CUSTOMER_MAX = 120
# Marks "the order had no customer key" apart from "customer: null".
_ABSENT = object()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _customer(order_no: str, order: dict) -> Any:
    """The order's customer name (stripped, cut to CUSTOMER_MAX), None when sent as
    null or blank, or _ABSENT when the key is not there at all."""
    if "customer" not in order:
        return _ABSENT
    raw = order["customer"]
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise UnprocessableError(f"{order_no}: customer must be text.", field="orders")
    return raw.strip()[:CUSTOMER_MAX] or None


def validate_customers(data: dict) -> dict[str, str | None]:
    """{order_no: name or None} for every order that carries a customer key. Call
    after validate_payload, which has already checked the overall shape."""
    customers: dict[str, str | None] = {}
    for order_no, order in data["orders"].items():
        value = _customer(str(order_no), order)
        if value is not _ABSENT:
            customers[str(order_no)] = value
    return customers


def validate_payload(data: Any) -> dict[str, list[dict]]:
    """Returns {order_no: [clean line dicts]} or raises UnprocessableError.
    The optional per-order customer is read by validate_customers."""
    if not isinstance(data, dict):
        raise UnprocessableError("Body must be a JSON object.")
    orders = data.get("orders")
    if not isinstance(orders, dict):
        raise UnprocessableError(
            "'orders' must be an object keyed by order number.", field="orders"
        )

    parsed: dict[str, list[dict]] = {}
    seen_ids: dict[int, str] = {}
    for order_no, order in orders.items():
        if not ORDER_NO_RE.match(str(order_no)):
            raise UnprocessableError(f"'{order_no}' is not an order number.", field="orders")
        if not isinstance(order, dict) or not isinstance(order.get("lines"), list):
            raise UnprocessableError(f"{order_no}: 'lines' must be a list.", field="orders")
        lines: list[dict] = []
        for raw in order["lines"]:
            if not isinstance(raw, dict):
                raise UnprocessableError(
                    f"{order_no}: each line must be an object.", field="orders"
                )
            detail_id = raw.get("order_detail_id")
            if not _is_int(detail_id) or detail_id <= 0:
                raise UnprocessableError(
                    f"{order_no}: order_detail_id must be a positive integer.", field="orders"
                )
            if detail_id in seen_ids:
                raise UnprocessableError(
                    f"order_detail_id {detail_id} appears more than once "
                    f"({seen_ids[detail_id]} and {order_no}).",
                    field="orders",
                )
            seen_ids[detail_id] = order_no
            line = (
                normalize_line_label(raw.get("line")) if isinstance(raw.get("line"), str) else None
            )
            if not line or len(line) > 10:
                raise UnprocessableError(
                    f"{order_no}/{detail_id}: 'line' must be a letter like 'A'.", field="orders"
                )
            qty = raw.get("qty")
            if qty is not None and (not _is_int(qty) or qty < 0):
                raise UnprocessableError(
                    f"{order_no}/{detail_id}: qty must be a whole number >= 0.", field="orders"
                )
            detail = raw.get("detail")
            if detail is not None and not isinstance(detail, dict):
                raise UnprocessableError(
                    f"{order_no}/{detail_id}: detail must be an object.", field="orders"
                )
            lines.append({"order_detail_id": detail_id, "line": line, "qty": qty, "detail": detail})
        parsed[str(order_no)] = lines
    return parsed


def process_payload(db: Session, data: Any) -> dict[str, int]:
    """Validate, replace-per-order, log one SyncLog row, commit.
    Returns {"orders": N, "lines": N} (counts in this snapshot)."""
    orders = validate_payload(data)
    customers = validate_customers(data)
    now = dt.datetime.now(dt.timezone.utc)
    created = updated = removed = backfilled = 0

    for order_no, name in customers.items():
        work_order = db.get(WorkOrder, order_no)
        if work_order is None:
            work_order = WorkOrder(order_no=order_no)
            db.add(work_order)
        work_order.customer_name = name
        work_order.received_at = now

    for order_no, lines in orders.items():
        keep_ids = {line["order_detail_id"] for line in lines}
        for stale in db.query(OrderLine).filter(OrderLine.order_no == order_no).all():
            if stale.order_detail_id not in keep_ids:
                db.delete(stale)
                removed += 1
        for line in lines:
            row = db.get(OrderLine, line["order_detail_id"])
            if row is None:
                row = OrderLine(order_detail_id=line["order_detail_id"])
                db.add(row)
                created += 1
            else:
                updated += 1
            row.order_no = order_no
            row.line = line["line"]
            row.qty = line["qty"]
            row.detail_json = json.dumps(line["detail"]) if line["detail"] is not None else None
            row.received_at = now
        db.flush()
        backfilled += _backfill_case_lines(db, order_no, lines)

    total_lines = sum(len(lines) for lines in orders.values())
    source = data.get("source") or "eagle-drawers-production-count"
    db.add(
        SyncLog(
            sync_started_at=now,
            sync_completed_at=dt.datetime.now(dt.timezone.utc),
            source_url=f"relay:{source}/order-lines"[:255],
            records_fetched=total_lines,
            records_created=created,
            records_updated=updated,
            records_skipped=0,
            errors="; ".join(
                note
                for note in (
                    f"{removed} line(s) no longer on their order were removed" if removed else "",
                    f"line letter filled in on {backfilled} case(s)" if backfilled else "",
                )
                if note
            )
            or None,
            status="success",
        )
    )
    db.commit()
    return {"orders": len(orders), "lines": total_lines}


def _backfill_case_lines(db: Session, order_no: str, lines: list[dict]) -> int:
    """Kickback cases can arrive before their order's lines do, and were saved with
    no line letter. Fill it in now: only on cases for the same drawer AND the same
    order (the label trust rule), only where the line is still blank. A line
    someone typed is never touched."""
    by_id = {line["order_detail_id"]: line["line"] for line in lines}
    if not by_id:
        return 0
    cases = (
        db.query(DefectCase)
        .filter(
            DefectCase.work_order_number == order_no,
            DefectCase.order_detail_id.in_(list(by_id)),
            (DefectCase.line_label.is_(None)) | (DefectCase.line_label == ""),
        )
        .all()
    )
    for case in cases:
        case.line_label = by_id[case.order_detail_id]
    return len(cases)


def format_spec(detail: dict | None) -> str | None:
    """'3.5 x 12.6875 x 20 · Maple · 1/4 bottom · Scoops Standard': the line's
    size, wood, bottom and options in one string for the floor cards. Empty parts
    are left out; None when there is nothing to show."""
    if not detail:
        return None
    size = str(detail.get("Size") or "").strip()
    wood = str(detail.get("Wood") or "").strip()
    bottom = str(detail.get("Bottom") or "").strip()
    options = str(detail.get("Options") or "").strip()
    parts = [
        size,
        wood[:1].upper() + wood[1:],
        f"{bottom} bottom" if bottom else "",
        options,
    ]
    return " · ".join(p for p in parts if p) or None


def drawer_info(db: Session, cases: list[DefectCase]) -> dict[int, dict]:
    """{case.id: {"customer", "line", "spec", "notes"}} for display, in three bulk
    queries. The line comes from, in order:
      1. the drawer's own order line (order_detail_id on the same order - the label
         trust rule), even when the case was saved before its lines arrived;
      2. the case's own line letter, matched to its order's line by letter.
    Size and options come from whichever line matched."""
    if not cases:
        return {}
    orders = {c.work_order_number for c in cases if c.work_order_number}
    detail_ids = {c.order_detail_id for c in cases if c.order_detail_id}
    customers: dict[str, str | None] = {}
    by_letter: dict[tuple[str, str], OrderLine] = {}
    if orders:
        for w in db.query(WorkOrder).filter(WorkOrder.order_no.in_(orders)).all():
            customers[w.order_no] = w.customer_name
        for r in db.query(OrderLine).filter(OrderLine.order_no.in_(orders)).all():
            by_letter.setdefault((r.order_no, r.line), r)
    by_id: dict[int, OrderLine] = {}
    if detail_ids:
        for r in db.query(OrderLine).filter(OrderLine.order_detail_id.in_(detail_ids)).all():
            by_id[r.order_detail_id] = r

    info: dict[int, dict] = {}
    for case in cases:
        row = by_id.get(case.order_detail_id) if case.order_detail_id else None
        if row is not None and row.order_no != case.work_order_number:
            row = None
        if row is None and case.line_label:
            row = by_letter.get((case.work_order_number, case.line_label))
        detail = detail_of(row) if row is not None else None
        notes = str(detail.get("Notes") or "").strip() if detail else ""
        info[case.id] = {
            "customer": customers.get(case.work_order_number),
            "line": row.line if row is not None else (case.line_label or None),
            "spec": format_spec(detail),
            "notes": notes or None,
        }
    return info


def customer_of(db: Session, order_no: str | None) -> str | None:
    if not order_no:
        return None
    row = db.get(WorkOrder, order_no)
    return row.customer_name if row is not None else None


def get_line(db: Session, order_detail_id: int) -> OrderLine | None:
    return db.get(OrderLine, order_detail_id)


def detail_of(row: OrderLine) -> dict | None:
    return json.loads(row.detail_json) if row.detail_json else None


def clamp_by_case(db: Session, cases: list[DefectCase]) -> dict[int, str]:
    """{case.id: clamp} for the cases a clamp is known for (2026-10 redesign):
    a kickback case takes the clamp on the event that opened it; any other case on
    an identified drawer takes the clamp on that drawer's latest event up to when
    the case was logged."""
    from app.models import DrawerEvent

    if not cases:
        return {}
    out: dict[int, str] = {}
    by_case = dict(
        db.query(DrawerEvent.defect_case_id, DrawerEvent.clamp)
        .filter(
            DrawerEvent.defect_case_id.in_([c.id for c in cases]),
            DrawerEvent.clamp.is_not(None),
        )
        .all()
    )
    drawers = {(c.order_detail_id, c.drawer_unit) for c in cases if c.order_detail_id}
    events: dict[tuple[int, int], list] = {}
    if drawers:
        rows = (
            db.query(
                DrawerEvent.order_detail_id,
                DrawerEvent.drawer_unit,
                DrawerEvent.occurred_at,
                DrawerEvent.clamp,
            )
            .filter(
                DrawerEvent.order_detail_id.in_([d for d, _u in drawers]),
                DrawerEvent.clamp.is_not(None),
            )
            .order_by(DrawerEvent.occurred_at)
            .all()
        )
        for detail_id, unit, occurred_at, clamp in rows:
            events.setdefault((detail_id, unit), []).append((occurred_at, clamp))
    for case in cases:
        if case.id in by_case:
            out[case.id] = by_case[case.id]
            continue
        if not case.order_detail_id:
            continue
        logged = (
            case.detected_at
            if case.detected_at.tzinfo
            else case.detected_at.replace(tzinfo=dt.timezone.utc)
        )
        latest = None
        for occurred_at, clamp in events.get((case.order_detail_id, case.drawer_unit), []):
            occurred = (
                occurred_at if occurred_at.tzinfo else occurred_at.replace(tzinfo=dt.timezone.utc)
            )
            if occurred <= logged:
                latest = clamp
        if latest:
            out[case.id] = latest
    return out
