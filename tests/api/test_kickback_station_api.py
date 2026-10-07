"""2026-10-07 fix: QC kickbacks were recorded at "Area 3".

Since the 09-03 duplicate incident a hidden leftover row ("Area 3") still holds
the built-in name "QC / Sorting / Shipping" while the real station was renamed
("QC / Sorting"). The kickback station is now chosen in Admin by id (like the
category), automatic lookups skip hidden rows, and the leftovers can be deleted.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.models import DefectCase, DefectCategory, Station

KEY = "test-relay-key-do-not-use-in-prod"
SETTINGS = "/api/v1/settings/undo-categories"


@pytest.fixture()
def relay_key(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def incident(client):
    """Reproduce the live rows: real station renamed, a hidden leftover holding the
    built-in name; same for the fallback category."""
    db = client.testing_sessionmaker()
    try:
        real = db.query(Station).filter(Station.seed_key == "QC / Sorting / Shipping").one()
        real.seed_key = None
        real.name = "QC / Sorting"
        leftover = Station(
            name="Area 3", seed_key="QC / Sorting / Shipping", active=False, sort_order=99
        )
        db.add(leftover)
        other = db.query(DefectCategory).filter(DefectCategory.seed_key == "Other").one()
        other.seed_key = None
        other_leftover = DefectCategory(
            name="Defect 16", seed_key="Other", active=False, sort_order=99
        )
        db.add(other_leftover)
        db.commit()
        return {"real": real.id, "leftover": leftover.id, "other_leftover": other_leftover.id}
    finally:
        db.close()


def _kickback(client, event_id=1):
    resp = client.post(
        "/api/v1/sync/drawer-events/ingest-raw",
        json={
            "source": "eagle-drawers-production-count",
            "events": [
                {
                    "event_id": event_id,
                    "type": "kickback",
                    "area": "qc",
                    "order_no": "179459",
                    "order_detail_id": 285016,
                    "unit": event_id,
                    "occurred_at": "2026-10-06T14:00:00+00:00",
                }
            ],
        },
        headers={"X-Relay-Key": KEY},
    )
    assert resp.status_code == 200, resp.text
    db = client.testing_sessionmaker()
    try:
        case = db.query(DefectCase).order_by(DefectCase.id.desc()).first()
        return case.found_station_id
    finally:
        db.close()


def test_the_admin_station_is_used(client, relay_key, incident):
    settings = client.get(SETTINGS).json()
    settings["qc_station_id"] = incident["real"]
    assert client.put(SETTINGS, json=settings).status_code == 200
    assert _kickback(client) == incident["real"]


def test_a_hidden_leftover_is_never_the_automatic_choice(client, relay_key, incident):
    # No station chosen and no live row with the built-in name: falls back to the
    # leftover only as a last resort - so Admin must pick it; but a LIVE row with
    # the built-in name always wins over a hidden one.
    db = client.testing_sessionmaker()
    try:
        live = Station(name="QC / Sorting / Shipping", active=True, sort_order=1)
        db.add(live)
        db.commit()
        live_id = live.id
    finally:
        db.close()
    assert _kickback(client) == live_id


def test_leftovers_can_be_deleted_once_settings_point_elsewhere(client, incident, master_data):
    settings = client.get(SETTINGS).json()
    settings.update(
        qc_station_id=incident["real"],
        qc_category_id=master_data["categories"]["Sanding / Surface"],
        assembly_category_id=master_data["categories"]["Sanding / Surface"],
    )
    assert client.put(SETTINGS, json=settings).status_code == 200
    assert client.delete(f"/api/v1/master-data/stations/{incident['leftover']}").status_code == 200
    resp = client.delete(f"/api/v1/master-data/defect-categories/{incident['other_leftover']}")
    assert resp.status_code == 200, resp.text
    # The real, chosen station is still protected.
    assert client.delete(f"/api/v1/master-data/stations/{incident['real']}").status_code == 400
