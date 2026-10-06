"""Drawer label QR payloads -> which drawer (PROJECT_SPEC_PHASE10.md Part 2).

The one place a label's QR text is parsed. The New Defect form's scanner
(app/static/js/label-scan.js) only decodes the QR; it POSTs the raw text to
/api/v1/labels/resolve, which calls resolve_label() here.

Two label generations are in use:
  - unique-ID labels:  .../WorkOrderPDFs/\\179459.pdf#drawer=285016-1
      order 179459, order_detail_id 285016 (Access order-line record id),
      unit 1 within that line;
  - old order-only labels: .../WorkOrderPDFs/\\178414.pdf - no fragment.
The backslash before the order number is malformed at source (Access
export), so one or more slashes/backslashes are tolerated there.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.services import order_line_service

ORDER_NUMBER_RE = re.compile(r"[\\/]+(\d{6})\.pdf", re.IGNORECASE)
# "#drawer=285016-1"; also tolerates the fragment arriving URL-encoded.
DRAWER_FRAGMENT_RE = re.compile(r"(?:#|%23)drawer(?:=|%3D)(\d+)-(\d+)", re.IGNORECASE)


def parse_label_text(text: str | None) -> dict:
    """{order_no, order_detail_id, unit} - order_no None when the text doesn't
    look like a work order label at all; the other two None for an old
    order-only label (or a fragment without an order number, which isn't
    trusted on its own)."""
    text = text or ""
    order_match = ORDER_NUMBER_RE.search(text)
    order_no = order_match.group(1) if order_match else None
    order_detail_id = unit = None
    drawer_match = DRAWER_FRAGMENT_RE.search(text)
    if order_no and drawer_match:
        order_detail_id = int(drawer_match.group(1))
        unit = int(drawer_match.group(2))
    return {"order_no": order_no, "order_detail_id": order_detail_id, "unit": unit}


def resolve_label(db: Session, text: str | None) -> dict:
    """parse_label_text() plus, when order_detail_id is known (pushed by
    production count) AND belongs to the same order, the line letter, qty and
    detail. line_known False means: fill the order number only and leave the
    A-Z line picker to the operator, exactly as for an order-only label."""
    parsed = parse_label_text(text)
    result = {
        **parsed,
        "line_known": False,
        "line_label": None,
        "qty": None,
        "detail": None,
        "customer_name": order_line_service.customer_of(db, parsed["order_no"]),
        "spec": None,
    }
    if parsed["order_detail_id"] is None:
        return result
    row = order_line_service.get_line(db, parsed["order_detail_id"])
    # A line pushed under a different order is a data mismatch - never put a
    # letter from someone else's order on this drawer.
    if row is None or row.order_no != parsed["order_no"]:
        return result
    result.update(
        line_known=True,
        line_label=row.line,
        qty=row.qty,
        detail=order_line_service.detail_of(row),
        spec=order_line_service.format_spec(order_line_service.detail_of(row)),
    )
    return result
