"""2026-10 redesign, phase 5: Admin's Delete / Restore for stations and categories.

Delete only hides the row (is_deleted) - CLAUDE.md: master data is never
hard-deleted. Hidden rows leave every picker, historical cases keep their names,
the seed loop never re-creates them, and the kickback station/categories can't be
deleted at all.
"""

from __future__ import annotations

from app.models import DefectCategory, Station
from app.seed_data import seed_master_data

MD = "/api/v1/master-data"


def _names(client, **params) -> tuple[set[str], set[str]]:
    data = client.get(MD, params=params).json()
    return {s["name"] for s in data["stations"]}, {c["name"] for c in data["defect_categories"]}


def test_deleted_category_leaves_every_list_and_restore_brings_it_back(client, master_data):
    cat_id = master_data["categories"]["Sanding / Surface"]
    resp = client.delete(f"{MD}/defect-categories/{cat_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_deleted"] is True
    assert "Sanding / Surface" not in _names(client)[1]
    assert "Sanding / Surface" not in _names(client, active_only=True)[1]
    assert "Sanding / Surface" in _names(client, include_deleted=True)[1]

    assert client.post(f"{MD}/defect-categories/{cat_id}/restore").status_code == 200
    assert "Sanding / Surface" in _names(client)[1]


def test_deleted_station_leaves_the_list(client, master_data):
    station_id = master_data["stations"]["Prep Sanding"]
    assert client.delete(f"{MD}/stations/{station_id}").status_code == 200
    assert "Prep Sanding" not in _names(client)[0]


def test_a_historical_case_keeps_the_deleted_categorys_name(client, master_data):
    cat_id = master_data["categories"]["Sanding / Surface"]
    created = client.post(
        "/api/v1/defect-cases",
        json={
            "production_date": "2026-10-06",
            "detected_at": "2026-10-06T14:30:00Z",
            "work_order_number": "179459",
            "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
            "priority": "Normal",
            "items": [{"defect_category_id": cat_id}],
        },
    ).json()
    client.delete(f"{MD}/defect-categories/{cat_id}")
    case = client.get(f"/api/v1/defect-cases/{created['id']}").json()
    assert case["items"][0]["defect_category_name"] == "Sanding / Surface"


def test_kickback_stations_cannot_be_deleted(client, master_data):
    for name in ("QC / Sorting / Shipping", "Assembly"):
        resp = client.delete(f"{MD}/stations/{master_data['stations'][name]}")
        assert resp.status_code == 400, name
        assert "kickback" in resp.json()["error"]["message"].lower()


def test_the_admin_kickback_category_cannot_be_deleted(client, master_data):
    cat_id = master_data["categories"]["Sanding / Surface"]
    client.put(
        "/api/v1/settings/undo-categories",
        json={"qc_category_id": cat_id, "assembly_category_id": None},
    )
    resp = client.delete(f"{MD}/defect-categories/{cat_id}")
    assert resp.status_code == 400
    assert "kickback category" in resp.json()["error"]["message"]


def test_the_fallback_category_cannot_be_deleted(client, master_data):
    resp = client.delete(f"{MD}/defect-categories/{master_data['categories']['Other']}")
    assert resp.status_code == 400


def test_reseeding_never_recreates_a_deleted_default(client, master_data):
    client.delete(f"{MD}/stations/{master_data['stations']['Prep Sanding']}")
    db = client.testing_sessionmaker()
    try:
        seed_master_data(db)
        db.commit()
        assert db.query(Station).filter(Station.name == "Prep Sanding").count() == 1
        assert db.query(Station).filter(Station.is_deleted.is_(True)).count() == 1
        assert db.query(DefectCategory).count() == len(master_data["categories"])
    finally:
        db.close()


def test_unknown_ids_are_404(client):
    assert client.delete(f"{MD}/stations/99999").status_code == 404
    assert client.post(f"{MD}/defect-categories/99999/restore").status_code == 404
