"""API tests: Phase 3 favorites - max-5 enforcement (per table, independently),
auto-assigned favorite_rank, the FAVORITES_ENABLED kill switch, and that an
inactive station never appears in the active-only favorites source, even
while still flagged is_favorite."""

from __future__ import annotations

import pytest


def _station_ids(client, limit=None):
    resp = client.get("/api/v1/master-data")
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()["stations"]]
    return ids[:limit] if limit else ids


def _category_ids(client, limit=None):
    resp = client.get("/api/v1/master-data")
    assert resp.status_code == 200
    ids = [c["id"] for c in resp.json()["defect_categories"]]
    return ids[:limit] if limit else ids


def _favorite_station(client, station_id):
    return client.patch(f"/api/v1/master-data/stations/{station_id}", json={"is_favorite": True})


def _unfavorite_station(client, station_id):
    return client.patch(f"/api/v1/master-data/stations/{station_id}", json={"is_favorite": False})


def test_favoriting_a_station_auto_assigns_rank_1(client, master_data):
    station_id = master_data["stations"]["Dado"]
    resp = _favorite_station(client, station_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_favorite"] is True
    assert body["favorite_rank"] == 1


def test_favoriting_in_order_assigns_increasing_ranks(client):
    ids = _station_ids(client, limit=3)
    ranks = []
    for station_id in ids:
        resp = _favorite_station(client, station_id)
        assert resp.status_code == 200, resp.text
        ranks.append(resp.json()["favorite_rank"])
    assert ranks == [1, 2, 3]


def test_max_5_favorites_enforced_per_table(client):
    ids = _station_ids(client, limit=6)
    assert len(ids) >= 6, "seed data must have at least 6 stations for this test"

    for station_id in ids[:5]:
        resp = _favorite_station(client, station_id)
        assert resp.status_code == 200, resp.text

    resp = _favorite_station(client, ids[5])
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "is_favorite"
    assert "unfavorite one first" in resp.json()["error"]["message"].lower()

    # The 6th station must NOT have been favorited by the rejected attempt.
    check = client.get("/api/v1/master-data")
    sixth = next(s for s in check.json()["stations"] if s["id"] == ids[5])
    assert sixth["is_favorite"] is False


def test_stations_and_categories_favorite_caps_are_independent(client):
    """Maxing out 5 favorite stations must not block favoriting categories, and
    vice versa - two separate tables, two separate caps."""
    station_ids = _station_ids(client, limit=5)
    for station_id in station_ids:
        assert _favorite_station(client, station_id).status_code == 200

    category_ids = _category_ids(client, limit=5)
    for category_id in category_ids:
        resp = client.patch(
            f"/api/v1/master-data/defect-categories/{category_id}", json={"is_favorite": True}
        )
        assert resp.status_code == 200, resp.text


def test_unfavoriting_frees_a_slot_for_a_new_favorite(client):
    ids = _station_ids(client, limit=6)
    for station_id in ids[:5]:
        assert _favorite_station(client, station_id).status_code == 200

    # 6th is rejected while all 5 slots are taken.
    assert _favorite_station(client, ids[5]).status_code == 400

    # Unfavorite one of the first five, freeing a slot.
    assert _unfavorite_station(client, ids[0]).status_code == 200

    # Now the 6th can be favorited.
    resp = _favorite_station(client, ids[5])
    assert resp.status_code == 200, resp.text


def test_unfavoriting_leaves_favorite_rank_unchanged(client, master_data):
    """Deliberate design choice (app/services/master_data_service.py
    _apply_favorite): unfavoriting doesn't clear favorite_rank, so re-favoriting
    later without a fresh explicit choice can just re-take its old slot."""
    station_id = master_data["stations"]["Dado"]
    fav_resp = _favorite_station(client, station_id)
    rank = fav_resp.json()["favorite_rank"]

    unfav_resp = _unfavorite_station(client, station_id)
    assert unfav_resp.status_code == 200
    assert unfav_resp.json()["is_favorite"] is False
    assert unfav_resp.json()["favorite_rank"] == rank


def test_setting_is_favorite_true_again_is_a_no_op(client, master_data):
    """Re-saving is_favorite=true on an already-favorited row must not re-enforce
    the cap or reassign its rank - it's already counted."""
    station_id = master_data["stations"]["Dado"]
    first = _favorite_station(client, station_id)
    rank = first.json()["favorite_rank"]

    again = _favorite_station(client, station_id)
    assert again.status_code == 200
    assert again.json()["favorite_rank"] == rank


def test_inactive_favorited_station_excluded_from_active_only_endpoint(client, master_data):
    """The New Defect page's favorites bar sources from the active-only
    endpoint (Phase 1) - an inactive station must disappear from it even while
    still flagged is_favorite ("active status still wins")."""
    station_id = master_data["stations"]["Dado"]
    assert _favorite_station(client, station_id).status_code == 200

    resp = client.patch(f"/api/v1/master-data/stations/{station_id}", json={"active": False})
    assert resp.status_code == 200
    assert resp.json()["is_favorite"] is True, "deactivating must not silently unfavorite"

    active_only = client.get("/api/v1/master-data", params={"active_only": "true"})
    ids = {s["id"] for s in active_only.json()["stations"]}
    assert station_id not in ids


def test_inactive_favorite_does_not_count_toward_the_cap(client):
    """The 5-cap counts ACTIVE favorited rows only (see
    master_data_service._apply_favorite) - a favorited-then-deactivated station
    must not permanently occupy one of the 5 slots."""
    ids = _station_ids(client, limit=6)
    for station_id in ids[:5]:
        assert _favorite_station(client, station_id).status_code == 200

    # Deactivate one of the five favorited (but don't unfavorite it).
    deactivate_resp = client.patch(f"/api/v1/master-data/stations/{ids[0]}", json={"active": False})
    assert deactivate_resp.status_code == 200
    assert deactivate_resp.json()["is_favorite"] is True

    # A 6th ACTIVE station can now be favorited - only 4 active favorites remain.
    resp = _favorite_station(client, ids[5])
    assert resp.status_code == 200, resp.text


def test_favoriting_nonexistent_station_is_404(client):
    resp = _favorite_station(client, 999999)
    assert resp.status_code == 404


@pytest.mark.parametrize("enabled", [True, False])
def test_master_data_reports_favorites_enabled_flag(client, monkeypatch, enabled):
    from app import config

    monkeypatch.setattr(config.get_settings(), "favorites_enabled", enabled)
    resp = client.get("/api/v1/master-data")
    assert resp.status_code == 200
    assert resp.json()["favorites_enabled"] is enabled


def test_favorites_enabled_defaults_false(client):
    """Ships dormant (app/config.py FAVORITES_ENABLED) - a deploy alone must
    never change what the shop floor sees."""
    from app import config

    assert config.get_settings().favorites_enabled is False
    resp = client.get("/api/v1/master-data")
    assert resp.json()["favorites_enabled"] is False
