"""API tests: active_only filtering on GET /api/v1/master-data (Phase 1 fix).

Root cause covered here: the endpoint used to return every station/category,
active or not, with no way to ask for only the active ones - so a station or
category toggled inactive in Admin kept showing up as a choice on the New
Defect form. See app/routers/master_data.py get_master_data.
"""

from __future__ import annotations


def test_default_call_still_returns_everything(client, master_data):
    """Reports/Dashboard/Admin/Rework Queue all call this with no active_only
    param and must keep seeing retired values for historical filtering -
    deactivating a station/category must not affect them."""
    station_id = master_data["stations"]["QC / Sorting / Shipping"]
    resp = client.patch(f"/api/v1/master-data/stations/{station_id}", json={"active": False})
    assert resp.status_code == 200, resp.text

    resp = client.get("/api/v1/master-data")
    assert resp.status_code == 200
    names = {s["name"] for s in resp.json()["stations"]}
    assert "QC / Sorting / Shipping" in names


def test_active_only_excludes_deactivated_station(client, master_data):
    station_id = master_data["stations"]["QC / Sorting / Shipping"]

    resp = client.get("/api/v1/master-data", params={"active_only": "true"})
    assert resp.status_code == 200
    names_before = {s["name"] for s in resp.json()["stations"]}
    assert "QC / Sorting / Shipping" in names_before

    resp = client.patch(f"/api/v1/master-data/stations/{station_id}", json={"active": False})
    assert resp.status_code == 200, resp.text

    resp = client.get("/api/v1/master-data", params={"active_only": "true"})
    assert resp.status_code == 200
    names_after = {s["name"] for s in resp.json()["stations"]}
    assert "QC / Sorting / Shipping" not in names_after
    # Every other still-active station stays present.
    assert names_after == names_before - {"QC / Sorting / Shipping"}


def test_active_only_excludes_deactivated_category(client, master_data):
    category_id = master_data["categories"]["Sanding / Surface"]

    resp = client.patch(
        f"/api/v1/master-data/defect-categories/{category_id}", json={"active": False}
    )
    assert resp.status_code == 200, resp.text

    resp = client.get("/api/v1/master-data", params={"active_only": "true"})
    assert resp.status_code == 200
    names = {c["name"] for c in resp.json()["defect_categories"]}
    assert "Sanding / Surface" not in names


def test_reactivating_brings_it_back_into_active_only(client, master_data):
    station_id = master_data["stations"]["QC / Sorting / Shipping"]
    client.patch(f"/api/v1/master-data/stations/{station_id}", json={"active": False})

    resp = client.get("/api/v1/master-data", params={"active_only": "true"})
    assert "QC / Sorting / Shipping" not in {s["name"] for s in resp.json()["stations"]}

    client.patch(f"/api/v1/master-data/stations/{station_id}", json={"active": True})

    resp = client.get("/api/v1/master-data", params={"active_only": "true"})
    assert "QC / Sorting / Shipping" in {s["name"] for s in resp.json()["stations"]}
