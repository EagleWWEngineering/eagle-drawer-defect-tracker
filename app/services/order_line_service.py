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
from app.models import OrderLine, SyncLog
from app.services.defect_service import normalize_line_label

ORDER_NO_RE = re.compile(r"^\d{1,20}$")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_payload(data: Any) -> dict[str, list[dict]]:
    """Returns {order_no: [clean line dicts]} or raises UnprocessableError."""
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
    now = dt.datetime.now(dt.timezone.utc)
    created = updated = removed = 0

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
            errors=f"{removed} line(s) no longer on their order were removed" if removed else None,
            status="success",
        )
    )
    db.commit()
    return {"orders": len(orders), "lines": total_lines}


def get_line(db: Session, order_detail_id: int) -> OrderLine | None:
    return db.get(OrderLine, order_detail_id)


def detail_of(row: OrderLine) -> dict | None:
    return json.loads(row.detail_json) if row.detail_json else None
