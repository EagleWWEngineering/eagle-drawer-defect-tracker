"""API tests for PROJECT_SPEC_PHASE10.md Part 2: unique-ID drawer labels.

- POST /api/v1/sync/order-lines/ingest-raw  (production count's hourly
  open-orders snapshot; X-Relay-Key; replace-per-order, never delete absent
  orders)
- POST /api/v1/labels/resolve               (the one place label QR text is
  parsed; behind the normal login)
- defect cases carrying order_detail_id + drawer_unit
"""

from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.models import OrderLine, SyncLog

INGEST_PATH = "/api/v1/sync/order-lines/ingest-raw"
RESOLVE_PATH = "/api/v1/labels/resolve"
TEST_RELAY_KEY = "test-relay-key-do-not-use-in-prod"

# Real QR payloads, as the scanner decodes them. The backslash before the order
# number is malformed at source and must be tolerated.
UNIQUE_ID_LABEL = (
    "https://eagledovetaildrawers.sharepoint.com/:b:/r/sites/Server/Documents/AccessDB/"
    "WorkOrderPDFs/\\179459.pdf#drawer=285016-1"
)
UNIQUE_ID_LABEL_DOUBLE_BACKSLASH = (
    "https://eagledovetaildrawers.sharepoint.com/:b:/r/sites/Server/Documents/AccessDB/"
    "WorkOrderPDFs/\\\\179459.pdf#drawer=285016-2"
)
ORDER_ONLY_LABEL = (
    "https://eagledovetaildrawers.sharepoint.com/:b:/r/sites/Server/Documents/AccessDB/"
    "WorkOrderPDFs/\\178414.pdf"
)

LINE_A = {
    "order_detail_id": 285016,
    "line": "A",
    "qty": 2,
    "detail": {
        "Size": "8 x 37.875 x 27",
        "Wood": "maple",
        "Bottom": "1/4",
        "Options": "N&B 2",
        "Notes": "",
    },
}
LINE_B = {"order_detail_id": 285017, "line": "B", "qty": 4, "detail": {"Size": "4 x 20 x 21"}}


def _snapshot(orders: dict) -> dict:
    return {
        "source": "eagle-drawers-production-count",
        "generated_at": "2026-09-30T19:00:00Z",
        "orders": orders,
    }


@pytest.fixture()
def relay_key(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", TEST_RELAY_KEY)
    get_settings.cache_clear()
    yield TEST_RELAY_KEY
    get_settings.cache_clear()


def _push(client, orders: dict, key: str | None = TEST_RELAY_KEY):
    headers = {"X-Relay-Key": key} if key is not None else {}
    return client.post(INGEST_PATH, json=_snapshot(orders), headers=headers)


def _lines(client) -> dict[int, OrderLine]:
    db = client.testing_sessionmaker()
    try:
        rows = db.query(OrderLine).all()
        db.expunge_all()
        return {r.order_detail_id: r for r in rows}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def test_ingest_stores_lines_keyed_by_order_detail_id(client, relay_key):
    resp = _push(client, {"179459": {"lines": [LINE_A, LINE_B]}, "179460": {"lines": []}})
    assert resp.status_code == 200
    assert resp.json() == {"orders": 2, "lines": 2}

    rows = _lines(client)
    assert set(rows) == {285016, 285017}
    a = rows[285016]
    assert (a.order_no, a.line, a.qty) == ("179459", "A", 2)
    assert json.loads(a.detail_json)["Wood"] == "maple"
    assert a.received_at is not None


def test_ingest_replaces_lines_of_each_order_in_the_body(client, relay_key):
    _push(client, {"179459": {"lines": [LINE_A, LINE_B]}})
    changed_a = {**LINE_A, "line": "C", "qty": 3}
    _push(client, {"179459": {"lines": [changed_a]}})  # B dropped from this order

    rows = _lines(client)
    assert set(rows) == {285016}
    assert (rows[285016].line, rows[285016].qty) == ("C", 3)


def test_ingest_never_deletes_orders_absent_from_the_snapshot(client, relay_key):
    _push(client, {"179459": {"lines": [LINE_A]}, "170001": {"lines": [LINE_B]}})
    # 170001 shipped - no longer in the open-orders snapshot.
    _push(client, {"179459": {"lines": [LINE_A]}})
    assert set(_lines(client)) == {285016, 285017}
    # And an empty snapshot changes nothing at all.
    assert _push(client, {}).json() == {"orders": 0, "lines": 0}
    assert set(_lines(client)) == {285016, 285017}


def test_ingest_is_idempotent(client, relay_key):
    body = {"179459": {"lines": [LINE_A, LINE_B]}}
    _push(client, body)
    first = {k: (r.order_no, r.line, r.qty, r.detail_json) for k, r in _lines(client).items()}
    _push(client, body)
    second = {k: (r.order_no, r.line, r.qty, r.detail_json) for k, r in _lines(client).items()}
    assert first == second


def test_ingest_normalises_line_letter(client, relay_key):
    _push(client, {"179459": {"lines": [{**LINE_A, "line": " a "}]}})
    assert _lines(client)[285016].line == "A"


def test_ingest_logs_one_sync_log_row(client, relay_key):
    _push(client, {"179459": {"lines": [LINE_A, LINE_B]}})
    db = client.testing_sessionmaker()
    logs = db.query(SyncLog).all()
    db.close()
    assert len(logs) == 1
    assert logs[0].status == "success"
    assert logs[0].source_url == "relay:eagle-drawers-production-count/order-lines"
    assert (logs[0].records_fetched, logs[0].records_created) == (2, 2)


@pytest.mark.parametrize("key", [None, "wrong-key"])
def test_ingest_rejects_missing_or_wrong_key(client, relay_key, key):
    client.cookies.clear()  # machine caller - no login session either
    assert _push(client, {"179459": {"lines": [LINE_A]}}, key=key).status_code == 401
    assert _lines(client) == {}


def test_ingest_works_without_a_login_session(client, relay_key):
    client.cookies.clear()
    assert _push(client, {"179459": {"lines": [LINE_A]}}).status_code == 200


@pytest.mark.parametrize(
    "orders",
    [
        {"17945X": {"lines": [LINE_A]}},  # not an order number
        {"179459": {"lines": "nope"}},
        {"179459": [LINE_A]},
        {"179459": {"lines": [{**LINE_A, "order_detail_id": "285016"}]}},
        {"179459": {"lines": [{**LINE_A, "order_detail_id": -1}]}},
        {"179459": {"lines": [{**LINE_A, "line": ""}]}},
        {"179459": {"lines": [{**LINE_A, "line": None}]}},
        {"179459": {"lines": [{**LINE_A, "qty": -2}]}},
        {"179459": {"lines": [{**LINE_A, "detail": "maple"}]}},
        {"179459": {"lines": [LINE_A]}, "179460": {"lines": [LINE_A]}},  # duplicate id
    ],
)
def test_ingest_invalid_entry_is_422_and_changes_nothing(client, relay_key, orders):
    _push(client, {"179459": {"lines": [LINE_B]}})
    before = {k: (r.order_no, r.line) for k, r in _lines(client).items()}
    resp = _push(client, {"179400": {"lines": [LINE_A]}, **orders})
    assert resp.status_code == 422
    assert {k: (r.order_no, r.line) for k, r in _lines(client).items()} == before


# ---------------------------------------------------------------------------
# Label resolve
# ---------------------------------------------------------------------------


def test_resolve_known_unique_id_label_fills_order_line_and_detail(client, relay_key):
    _push(client, {"179459": {"lines": [LINE_A]}})
    body = client.post(RESOLVE_PATH, json={"text": UNIQUE_ID_LABEL}).json()
    assert body == {
        "order_no": "179459",
        "order_detail_id": 285016,
        "unit": 1,
        "line_known": True,
        "line_label": "A",
        "qty": 2,
        "detail": LINE_A["detail"],
    }


def test_resolve_tolerates_the_malformed_double_backslash(client, relay_key):
    _push(client, {"179459": {"lines": [LINE_A]}})
    body = client.post(RESOLVE_PATH, json={"text": UNIQUE_ID_LABEL_DOUBLE_BACKSLASH}).json()
    assert (body["order_no"], body["order_detail_id"], body["unit"]) == ("179459", 285016, 2)
    assert body["line_label"] == "A"


def test_resolve_unknown_order_detail_id_fills_order_only(client):
    body = client.post(RESOLVE_PATH, json={"text": UNIQUE_ID_LABEL}).json()
    assert body["order_no"] == "179459"
    assert (body["order_detail_id"], body["unit"]) == (285016, 1)
    assert body["line_known"] is False
    assert body["line_label"] is None
    assert body["detail"] is None


def test_resolve_order_only_label_works_as_before(client, relay_key):
    _push(client, {"179459": {"lines": [LINE_A]}})
    body = client.post(RESOLVE_PATH, json={"text": ORDER_ONLY_LABEL}).json()
    assert body["order_no"] == "178414"
    assert body["order_detail_id"] is None
    assert body["unit"] is None
    assert body["line_known"] is False


def test_resolve_line_pushed_under_another_order_is_not_trusted(client, relay_key):
    _push(client, {"170000": {"lines": [LINE_A]}})  # 285016 belongs to 170000 here
    body = client.post(RESOLVE_PATH, json={"text": UNIQUE_ID_LABEL}).json()
    assert body["order_no"] == "179459"
    assert body["line_known"] is False
    assert body["line_label"] is None


def test_resolve_non_label_qr(client):
    body = client.post(RESOLVE_PATH, json={"text": "https://example.com/menu"}).json()
    assert body["order_no"] is None
    assert body["line_known"] is False


def test_resolve_requires_login(client):
    client.cookies.clear()
    assert client.post(RESOLVE_PATH, json={"text": UNIQUE_ID_LABEL}).status_code == 401


# ---------------------------------------------------------------------------
# Defect case drawer identity
# ---------------------------------------------------------------------------


def _case_payload(master_data, **overrides):
    payload = {
        "production_date": "2026-09-30",
        "detected_at": "2026-09-30T14:30:00Z",
        "work_order_number": "179459",
        "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
        "priority": "Normal",
        "items": [{"defect_category_id": master_data["categories"]["Sanding / Surface"]}],
    }
    payload.update(overrides)
    return payload


def test_defect_case_stores_drawer_identity(client, master_data):
    resp = client.post(
        "/api/v1/defect-cases",
        json=_case_payload(
            master_data,
            line_label="A",
            entry_source="scanned",
            order_detail_id=285016,
            drawer_unit=1,
        ),
    )
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert (created["order_detail_id"], created["drawer_unit"]) == (285016, 1)
    fetched = client.get(f"/api/v1/defect-cases/{created['id']}").json()
    assert (fetched["order_detail_id"], fetched["drawer_unit"]) == (285016, 1)


def test_manual_defect_case_has_null_drawer_identity(client, master_data):
    created = client.post("/api/v1/defect-cases", json=_case_payload(master_data)).json()
    assert created["order_detail_id"] is None
    assert created["drawer_unit"] is None


def test_drawer_unit_without_order_detail_id_is_rejected(client, master_data):
    resp = client.post("/api/v1/defect-cases", json=_case_payload(master_data, drawer_unit=1))
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "drawer_unit"
