"""2026-10-07 hotfix: QC kickbacks were recorded at "Area 3" (a hidden leftover
that still holds the built-in name "QC / Sorting / Shipping")."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.models import AppSetting, DefectCase, Station

KEY = "test-relay-key-do-not-use-in-prod"


@pytest.fixture()
def relay_key(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _kickback(client):
    resp = client.post(
        "/api/v1/sync/drawer-events/ingest-raw",
        json={
            "source": "eagle-drawers-production-count",
            "events": [
                {
                    "event_id": 1,
                    "type": "kickback",
                    "area": "qc",
                    "order_no": "179459",
                    "order_detail_id": 285016,
                    "unit": 1,
                    "occurred_at": "2026-10-06T14:00:00+00:00",
                }
            ],
        },
        headers={"X-Relay-Key": KEY},
    )
    assert resp.status_code == 200, resp.text
    db = client.testing_sessionmaker()
    try:
        return db.query(DefectCase).one().found_station_id
    finally:
        db.close()


def _incident(client, *, set_station: bool):
    db = client.testing_sessionmaker()
    try:
        real = db.query(Station).filter(Station.seed_key == "QC / Sorting / Shipping").one()
        real.seed_key, real.name = None, "QC / Sorting"
        db.add(
            Station(name="Area 3", seed_key="QC / Sorting / Shipping", active=False, sort_order=99)
        )
        if set_station:
            db.add(AppSetting(key="undo_station_qc", value=str(real.id)))
        db.commit()
        return real.id
    finally:
        db.close()


def test_the_set_station_is_used(client, relay_key):
    real = _incident(client, set_station=True)
    assert _kickback(client) == real


def test_an_active_row_beats_a_hidden_leftover(client, relay_key):
    db = client.testing_sessionmaker()
    try:
        db.add(
            Station(name="Area 3", seed_key="QC / Sorting / Shipping", active=False, sort_order=99)
        )
        db.commit()
        live = (
            db.query(Station)
            .filter(Station.name == "QC / Sorting / Shipping", Station.active.is_(True))
            .one()
            .id
        )
    finally:
        db.close()
    assert _kickback(client) == live
