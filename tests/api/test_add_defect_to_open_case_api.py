"""2026-10 redesign, phase 3: New Defect's duplicate warning and "Add to that case".

- GET /api/v1/defect-cases/open-for-drawer   open cases on a drawer / a work order
- POST /api/v1/defect-cases/{id}/add-defect  the new defect goes on the open case
  (categories added next to the existing ones - a kickback's "QC kickback"
  category stays; notes appended; priority only goes up; optional fixed-on-the-spot
  close through the normal status map)
"""

from __future__ import annotations

import pytest

OPEN = "/api/v1/defect-cases/open-for-drawer"


@pytest.fixture()
def drawer_case(client, master_data):
    payload = {
        "production_date": "2026-10-06",
        "detected_at": "2026-10-06T14:30:00Z",
        "work_order_number": "179459",
        "line_label": "A",
        "entry_source": "scanned",
        "order_detail_id": 285016,
        "drawer_unit": 1,
        "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
        "priority": "High",
        "notes": "kicked back at QC",
        "items": [{"defect_category_id": master_data["categories"]["Sanding / Surface"]}],
    }
    resp = client.post("/api/v1/defect-cases", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _category(master_data, index: int) -> int:
    names = [n for n in master_data["categories"] if n != "Sanding / Surface"]
    return master_data["categories"][names[index]]


def test_open_for_drawer_finds_the_drawers_case(client, drawer_case):
    rows = client.get(OPEN, params={"order_detail_id": 285016, "unit": 1}).json()
    assert [r["case_number"] for r in rows] == [drawer_case["case_number"]]
    assert rows[0]["categories"] == ["Sanding / Surface"]
    assert client.get(OPEN, params={"order_detail_id": 285016, "unit": 2}).json() == []


def test_open_for_drawer_by_work_order(client, drawer_case):
    rows = client.get(OPEN, params={"work_order_number": "179459"}).json()
    assert [r["id"] for r in rows] == [drawer_case["id"]]


def test_open_for_drawer_needs_a_drawer_or_work_order(client):
    assert client.get(OPEN).status_code == 400


def test_add_defect_keeps_existing_categories_and_appends_notes(client, master_data, drawer_case):
    new_cat = _category(master_data, 0)
    resp = client.post(
        f"/api/v1/defect-cases/{drawer_case['id']}/add-defect",
        json={"items": [{"defect_category_id": new_cat}], "notes": "glue on the back"},
    )
    assert resp.status_code == 200, resp.text
    case = resp.json()
    names = {i["defect_category_name"] for i in case["items"]}
    assert "Sanding / Surface" in names and len(names) == 2
    assert case["notes"] == "kicked back at QC; glue on the back"
    assert case["status"] == "Open"
    assert case["priority"] == "High"


def test_add_defect_only_raises_priority(client, master_data, drawer_case):
    url = f"/api/v1/defect-cases/{drawer_case['id']}/add-defect"
    item = {"defect_category_id": _category(master_data, 0)}
    assert (
        client.post(url, json={"items": [item], "priority": "Normal"}).json()["priority"] == "High"
    )
    assert (
        client.post(url, json={"items": [item], "priority": "Urgent"}).json()["priority"]
        == "Urgent"
    )


def test_add_defect_fixed_on_the_spot_closes_the_case(client, master_data, drawer_case):
    resp = client.post(
        f"/api/v1/defect-cases/{drawer_case['id']}/add-defect",
        json={
            "items": [{"defect_category_id": _category(master_data, 1)}],
            "instant_close_outcome": "Repaired",
            "repair_action": "Reglued",
        },
    )
    assert resp.status_code == 200, resp.text
    case = resp.json()
    assert case["status"] == "Closed - Repaired"
    assert case["repair_action"] == "Reglued"


def test_add_defect_fixed_on_the_spot_needs_what_was_done(client, master_data, drawer_case):
    resp = client.post(
        f"/api/v1/defect-cases/{drawer_case['id']}/add-defect",
        json={
            "items": [{"defect_category_id": _category(master_data, 1)}],
            "instant_close_outcome": "Repaired",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "repair_action"


def test_add_defect_refuses_a_closed_case(client, master_data, drawer_case):
    client.post(
        f"/api/v1/defect-cases/{drawer_case['id']}/status",
        json={"new_status": "Closed - Repaired", "note": "done"},
    )
    resp = client.post(
        f"/api/v1/defect-cases/{drawer_case['id']}/add-defect",
        json={"items": [{"defect_category_id": _category(master_data, 0)}]},
    )
    assert resp.status_code == 400
