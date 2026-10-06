"""2026-10 redesign, phase 1: customer names and drawer details for display.

- the order-lines snapshot may carry a "customer" per order (optional - an old
  payload without it is still accepted and leaves stored names alone)
- kickback cases saved before their lines arrived get the line letter filled in
- order_line_service.drawer_info: customer, line, size/options for any case
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.config import get_settings
from app.models import DefectCase, Station, WorkOrder
from app.services import order_line_service

INGEST_PATH = "/api/v1/sync/order-lines/ingest-raw"
TEST_RELAY_KEY = "test-relay-key-do-not-use-in-prod"

LINE_A = {
    "order_detail_id": 285016,
    "line": "A",
    "qty": 2,
    "detail": {
        "Size": "8 x 37.875 x 27",
        "Wood": "maple",
        "Bottom": "1/4",
        "Options": "N&B 2",
        "Notes": "Spice drawer",
    },
}
LINE_B = {"order_detail_id": 285017, "line": "B", "qty": 4, "detail": {"Size": "4 x 20 x 21"}}


@pytest.fixture()
def relay_key(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", TEST_RELAY_KEY)
    get_settings.cache_clear()
    yield TEST_RELAY_KEY
    get_settings.cache_clear()


def _push(client, orders: dict):
    body = {"source": "eagle-drawers-production-count", "orders": orders}
    return client.post(INGEST_PATH, json=body, headers={"X-Relay-Key": TEST_RELAY_KEY})


def _customers(client) -> dict[str, str | None]:
    db = client.testing_sessionmaker()
    try:
        return {w.order_no: w.customer_name for w in db.query(WorkOrder).all()}
    finally:
        db.close()


def test_customer_is_stored_per_order(client, relay_key):
    resp = _push(client, {"179459": {"customer": "  Smith Cabinets ", "lines": [LINE_A]}})
    assert resp.status_code == 200, resp.text
    assert _customers(client) == {"179459": "Smith Cabinets"}


def test_payload_without_customer_is_still_accepted_and_keeps_the_stored_name(client, relay_key):
    _push(client, {"179459": {"customer": "Smith Cabinets", "lines": [LINE_A]}})
    assert _push(client, {"179459": {"lines": [LINE_A]}}).status_code == 200
    assert _customers(client) == {"179459": "Smith Cabinets"}


def test_blank_customer_clears_it(client, relay_key):
    _push(client, {"179459": {"customer": "Smith Cabinets", "lines": [LINE_A]}})
    _push(client, {"179459": {"customer": "  ", "lines": [LINE_A]}})
    assert _customers(client) == {"179459": None}


def test_customer_that_is_not_text_is_422_and_changes_nothing(client, relay_key):
    resp = _push(client, {"179459": {"customer": 42, "lines": [LINE_A]}})
    assert resp.status_code == 422
    assert _customers(client) == {}


def _add_case(client, **fields) -> int:
    db = client.testing_sessionmaker()
    try:
        station = db.query(Station).first()
        case = DefectCase(
            case_number=fields.pop("case_number", "DF-TEST-0001"),
            production_date=dt.date(2026, 10, 6),
            detected_at=dt.datetime(2026, 10, 6, 14, 0),
            found_station_id=station.id,
            priority="Normal",
            status="Open",
            **fields,
        )
        db.add(case)
        db.commit()
        return case.id
    finally:
        db.close()


def _case(client, case_id: int) -> DefectCase:
    db = client.testing_sessionmaker()
    try:
        case = db.get(DefectCase, case_id)
        db.expunge(case)
        return case
    finally:
        db.close()


def test_kickback_saved_before_lines_gets_its_line_filled_in(client, relay_key):
    case_id = _add_case(
        client, work_order_number="179459", order_detail_id=285016, drawer_unit=1, line_label=None
    )
    _push(client, {"179459": {"lines": [LINE_A, LINE_B]}})
    assert _case(client, case_id).line_label == "A"


def test_backfill_never_overwrites_a_line_already_set(client, relay_key):
    case_id = _add_case(
        client, work_order_number="179459", order_detail_id=285016, drawer_unit=1, line_label="C"
    )
    _push(client, {"179459": {"lines": [LINE_A]}})
    assert _case(client, case_id).line_label == "C"


def test_backfill_ignores_a_drawer_id_on_another_order(client, relay_key):
    case_id = _add_case(
        client, work_order_number="999999", order_detail_id=285016, drawer_unit=1, line_label=None
    )
    _push(client, {"179459": {"lines": [LINE_A]}})
    assert _case(client, case_id).line_label is None


def test_drawer_info_from_the_drawer_id(client, relay_key):
    _push(client, {"179459": {"customer": "Smith Cabinets", "lines": [LINE_A, LINE_B]}})
    case_id = _add_case(
        client, work_order_number="179459", order_detail_id=285016, drawer_unit=1, line_label=None
    )
    db = client.testing_sessionmaker()
    try:
        info = order_line_service.drawer_info(db, [db.get(DefectCase, case_id)])[case_id]
    finally:
        db.close()
    assert info == {
        "customer": "Smith Cabinets",
        "line": "A",
        "spec": "8 x 37.875 x 27 · Maple · 1/4 bottom · N&B 2",
        "notes": "Spice drawer",
    }


def test_drawer_info_from_a_typed_line_letter(client, relay_key):
    _push(client, {"179459": {"customer": "Smith Cabinets", "lines": [LINE_A, LINE_B]}})
    case_id = _add_case(client, work_order_number="179459", line_label="B")
    db = client.testing_sessionmaker()
    try:
        info = order_line_service.drawer_info(db, [db.get(DefectCase, case_id)])[case_id]
    finally:
        db.close()
    assert (info["line"], info["spec"]) == ("B", "4 x 20 x 21")


def test_drawer_info_with_only_a_work_order(client, relay_key):
    _push(client, {"179459": {"customer": "Smith Cabinets", "lines": [LINE_A]}})
    case_id = _add_case(client, work_order_number="179459")
    db = client.testing_sessionmaker()
    try:
        info = order_line_service.drawer_info(db, [db.get(DefectCase, case_id)])[case_id]
    finally:
        db.close()
    assert info == {"customer": "Smith Cabinets", "line": None, "spec": None, "notes": None}
