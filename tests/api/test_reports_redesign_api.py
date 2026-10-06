"""2026-10 redesign, phase 7: Reports.

- kind=kickback|qc narrows every report (summary, Pareto, trend, records, CSV)
- Pareto groups by found station and by line too
- the trend follows every filter (it used to follow the dates only)
- open-aging and shop-vs-customer charts
- records and CSV carry customer and size
"""

from __future__ import annotations

import csv
import datetime as dt
import io

import pytest

from app.config import get_settings
from app.models import CustomerIssue, CustomerIssueCategory, DefectCase

TEST_RELAY_KEY = "test-relay-key-do-not-use-in-prod"
DAY = "2026-10-05"
RANGE = {"start_date": DAY, "end_date": DAY}


@pytest.fixture()
def relay_key(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", TEST_RELAY_KEY)
    get_settings.cache_clear()
    yield TEST_RELAY_KEY
    get_settings.cache_clear()


@pytest.fixture()
def cases(client, relay_key, master_data):
    client.post(
        "/api/v1/sync/order-lines/ingest-raw",
        json={
            "orders": {
                "179459": {
                    "customer": "Smith Cabinets",
                    "lines": [
                        {
                            "order_detail_id": 285016,
                            "line": "A",
                            "detail": {"Size": "8 x 37 x 27", "Wood": "maple"},
                        }
                    ],
                }
            }
        },
        headers={"X-Relay-Key": TEST_RELAY_KEY},
    )
    sanding = master_data["categories"]["Sanding / Surface"]
    other = master_data["categories"]["Other"]
    qc = master_data["stations"]["QC / Sorting / Shipping"]
    assembly = master_data["stations"]["Assembly"]

    def make(**overrides):
        payload = {
            "production_date": DAY,
            "detected_at": f"{DAY}T14:30:00Z",
            "work_order_number": "179459",
            "found_station_id": qc,
            "priority": "Normal",
            "items": [{"defect_category_id": sanding}],
        }
        payload.update(overrides)
        resp = client.post("/api/v1/defect-cases", json=payload)
        assert resp.status_code == 200, resp.text
        return resp.json()["id"]

    ids = {
        "qc_a": make(line_label="A", entry_source="scanned", order_detail_id=285016, drawer_unit=1),
        "qc_noline": make(work_order_number="180000", items=[{"defect_category_id": other}]),
        "kick": make(found_station_id=assembly),
    }
    db = client.testing_sessionmaker()
    try:
        db.get(DefectCase, ids["kick"]).entry_source = "undo_card"
        db.commit()
    finally:
        db.close()
    return ids


def test_kind_filter_narrows_the_summary_and_records(client, cases):
    assert client.get("/api/v1/reports/summary", params={**RANGE}).json()["total_cases"] == 3
    kick = client.get("/api/v1/reports/summary", params={**RANGE, "kind": "kickback"}).json()
    qc = client.get("/api/v1/reports/summary", params={**RANGE, "kind": "qc"}).json()
    assert (kick["total_cases"], qc["total_cases"]) == (1, 2)
    records = client.get("/api/v1/defect-cases", params={**RANGE, "kind": "kickback"}).json()
    assert [c["id"] for c in records["cases"]] == [cases["kick"]]


def test_pareto_by_found_station_and_by_line(client, cases):
    by_station = client.get(
        "/api/v1/reports/pareto", params={**RANGE, "group_by": "found_station"}
    ).json()
    assert {r["label"]: r["defect_events"] for r in by_station} == {
        "QC / Sorting / Shipping": 2,
        "Assembly": 1,
    }
    by_line = client.get("/api/v1/reports/pareto", params={**RANGE, "group_by": "line"}).json()
    assert {r["label"]: r["defect_events"] for r in by_line} == {"Line A": 1, "No line": 2}


def test_trend_follows_the_filters(client, cases):
    everything = client.get("/api/v1/reports/trend", params=RANGE).json()
    kickbacks = client.get("/api/v1/reports/trend", params={**RANGE, "kind": "kickback"}).json()
    assert everything[0]["defect_events"] == 3
    assert kickbacks[0]["defect_events"] == 1


def test_open_aging_buckets_open_cases(client, cases):
    db = client.testing_sessionmaker()
    try:
        now = dt.datetime.now(dt.timezone.utc)
        db.get(DefectCase, cases["qc_a"]).detected_at = now - dt.timedelta(hours=1)
        db.get(DefectCase, cases["qc_noline"]).detected_at = now - dt.timedelta(hours=30)
        db.get(DefectCase, cases["kick"]).detected_at = now - dt.timedelta(days=5)
        db.commit()
    finally:
        db.close()
    body = client.get("/api/v1/reports/open-aging", params=RANGE).json()
    assert body["open_cases"] == 3
    assert [b["count"] for b in body["buckets"]] == [1, 0, 1, 1]
    assert body["oldest_hours"] >= 120


def test_shop_vs_customer_ranks_each_side(client, cases):
    db = client.testing_sessionmaker()
    try:
        category = db.query(CustomerIssueCategory).first()
        db.add(
            CustomerIssue(
                issue_number="CI-1",
                reported_date=dt.date(2026, 10, 5),
                customer_name="Smith Cabinets",
                issue_category_id=category.id,
                source_type="Manufacturing",
                piece_count=2,
                description="scratched",
                status="Open",
            )
        )
        db.commit()
        category_name = category.name
    finally:
        db.close()
    body = client.get("/api/v1/reports/shop-vs-customer", params=RANGE).json()
    assert body["shop"][0] == {"label": "Sanding / Surface", "count": 2}
    assert body["customer"] == [{"label": category_name, "count": 2}]


def test_records_and_csv_carry_customer_and_size(client, cases):
    rows = client.get("/api/v1/defect-cases", params=RANGE).json()["cases"]
    row = next(r for r in rows if r["id"] == cases["qc_a"])
    assert (row["customer_name"], row["resolved_line"], row["spec"]) == (
        "Smith Cabinets",
        "A",
        "8 x 37 x 27 · Maple",
    )
    text = client.get("/api/v1/exports/defects.csv", params={**RANGE, "kind": "qc"}).text
    reader = list(csv.DictReader(io.StringIO(text)))
    assert len(reader) == 2
    first = next(r for r in reader if r["line_label"] == "A")
    assert (first["case_type"], first["customer"], first["size_and_options"]) == (
        "qc_defect",
        "Smith Cabinets",
        "8 x 37 x 27 · Maple",
    )
