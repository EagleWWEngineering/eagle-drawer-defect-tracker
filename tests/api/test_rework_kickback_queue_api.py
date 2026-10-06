"""2026-10 redesign, phase 2: GET /api/v1/rework-queue for the Rework & Kickback Queue.

Rows carry customer / line / size so the floor can find the drawer, say whether
they are kickbacks, and the list can be searched, narrowed to one scanned drawer,
or widened to recently closed cases.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.config import get_settings
from app.models import DefectCase, DrawerEvent, Station

QUEUE = "/api/v1/rework-queue"
TEST_RELAY_KEY = "test-relay-key-do-not-use-in-prod"
LINE_A = {
    "order_detail_id": 285016,
    "line": "A",
    "qty": 2,
    "detail": {"Size": "8 x 37.875 x 27", "Wood": "maple", "Bottom": "1/4", "Options": "N&B 2"},
}


@pytest.fixture()
def relay_key(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", TEST_RELAY_KEY)
    get_settings.cache_clear()
    yield TEST_RELAY_KEY
    get_settings.cache_clear()


@pytest.fixture()
def seeded(client, relay_key, master_data):
    client.post(
        "/api/v1/sync/order-lines/ingest-raw",
        json={"orders": {"179459": {"customer": "Smith Cabinets", "lines": [LINE_A]}}},
        headers={"X-Relay-Key": TEST_RELAY_KEY},
    )
    category = master_data["categories"]["Sanding / Surface"]
    station = master_data["stations"]["QC / Sorting / Shipping"]

    def case(**overrides):
        payload = {
            "production_date": "2026-10-06",
            "detected_at": "2026-10-06T14:30:00Z",
            "work_order_number": "179459",
            "found_station_id": station,
            "priority": "Normal",
            "items": [{"defect_category_id": category}],
        }
        payload.update(overrides)
        resp = client.post("/api/v1/defect-cases", json=payload)
        assert resp.status_code == 200, resp.text
        return resp.json()["id"]

    ids = {
        "drawer": case(
            line_label="A", entry_source="scanned", order_detail_id=285016, drawer_unit=1
        ),
        "other": case(work_order_number="180000", priority="High"),
    }
    return ids


def _ids(resp) -> list[int]:
    assert resp.status_code == 200, resp.text
    return [row["id"] for row in resp.json()]


def test_rows_carry_customer_line_and_size(client, seeded):
    row = next(r for r in client.get(QUEUE).json() if r["id"] == seeded["drawer"])
    assert row["customer_name"] == "Smith Cabinets"
    assert row["resolved_line"] == "A"
    assert row["spec"] == "8 x 37.875 x 27 · Maple · 1/4 bottom · N&B 2"
    assert row["kind"] == "qc"
    assert row["is_closed"] is False


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("smith", ["drawer"]),
        ("SMITH maple", ["drawer"]),
        ("line a", ["drawer"]),
        ("180000", ["other"]),
        ("sanding", ["other", "drawer"]),  # High priority sorts first
        ("smith 180000", []),
    ],
)
def test_search_matches_every_word(client, seeded, q, expected):
    assert _ids(client.get(QUEUE, params={"q": q})) == [seeded[k] for k in expected]


def test_drawer_filter_narrows_to_one_drawer(client, seeded):
    assert _ids(client.get(QUEUE, params={"drawer": "285016-1"})) == [seeded["drawer"]]
    assert _ids(client.get(QUEUE, params={"drawer": "285016-2"})) == []
    assert client.get(QUEUE, params={"drawer": "nope"}).status_code == 400


def test_closed_cases_only_with_include_closed(client, seeded):
    resp = client.post(
        f"/api/v1/defect-cases/{seeded['drawer']}/status",
        json={"new_status": "Closed - Repaired", "note": "reglued"},
    )
    assert resp.status_code == 200, resp.text
    assert seeded["drawer"] not in _ids(client.get(QUEUE))
    rows = client.get(QUEUE, params={"include_closed_days": 7}).json()
    assert [r["id"] for r in rows] == [seeded["other"], seeded["drawer"]]  # open first
    closed = rows[1]
    assert closed["is_closed"] is True and closed["closed_at_local"]


def test_closed_long_ago_stays_out(client, seeded):
    client.post(
        f"/api/v1/defect-cases/{seeded['drawer']}/status",
        json={"new_status": "Closed - Repaired", "note": "reglued"},
    )
    db = client.testing_sessionmaker()
    try:
        case = db.get(DefectCase, seeded["drawer"])
        case.closed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
        db.commit()
    finally:
        db.close()
    assert seeded["drawer"] not in _ids(client.get(QUEUE, params={"include_closed_days": 7}))


def test_kickback_rows_say_where_the_undo_card_was_used(client, seeded):
    db = client.testing_sessionmaker()
    try:
        station = db.query(Station).first()
        case = DefectCase(
            case_number="DF-20261006-0099",
            production_date=dt.date(2026, 10, 6),
            detected_at=dt.datetime(2026, 10, 6, 15, 0),
            work_order_number="179459",
            found_station_id=station.id,
            priority="Normal",
            status="Open",
            disposition="Set Aside",
            entry_source="undo_card",
            order_detail_id=285016,
            drawer_unit=2,
        )
        db.add(case)
        db.flush()
        db.add(
            DrawerEvent(
                source="eagle-drawers-production-count",
                event_id=1,
                event_type="kickback",
                area="assembly",
                order_no="179459",
                order_detail_id=285016,
                drawer_unit=2,
                occurred_at=dt.datetime(2026, 10, 6, 15, 0, tzinfo=dt.timezone.utc),
                received_at=dt.datetime(2026, 10, 6, 15, 0, tzinfo=dt.timezone.utc),
                outcome="opened",
                defect_case_id=case.id,
            )
        )
        db.commit()
        case_id = case.id
    finally:
        db.close()
    row = next(r for r in client.get(QUEUE).json() if r["id"] == case_id)
    assert (row["kind"], row["kickback_area"], row["resolved_line"]) == (
        "kickback",
        "assembly",
        "A",
    )
    assert _ids(client.get(QUEUE, params={"q": "kickback"})) == [case_id]
