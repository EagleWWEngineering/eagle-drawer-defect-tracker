"""API tests: Phase 2 defect-case editing - found station / possible source
validation, defect-item add/edit/remove (blocked on a closed case), and photo
deletion (DB row + file, both removed)."""

from __future__ import annotations

import io

from app.models import AuditLog, DefectPhoto


def _create_case(client, master_data, **overrides):
    payload = {
        "production_date": "2026-07-24",
        "detected_at": "2026-07-24T14:30:00Z",
        "work_order_number": "WO-7001",
        "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
        "priority": "Normal",
        "items": [
            {
                "defect_category_id": master_data["categories"]["Sanding / Surface"],
                "affected_drawer_quantity": 1,
            },
            {
                "defect_category_id": master_data["categories"]["Dado / Bottom Groove"],
                "affected_drawer_quantity": 1,
            },
        ],
    }
    payload.update(overrides)
    return client.post("/api/v1/defect-cases", json=payload)


def _audit_actions(client, entity_id):
    session = client.testing_sessionmaker()
    try:
        return [
            a.action
            for a in session.query(AuditLog)
            .filter(AuditLog.entity_type == "DefectCase", AuditLog.entity_id == entity_id)
            .all()
        ]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Found station / possible source: edit + existence validation
# ---------------------------------------------------------------------------


def test_found_station_and_possible_source_are_editable(client, master_data):
    case = _create_case(client, master_data).json()
    other_station_id = master_data["stations"]["Dado"]

    resp = client.patch(
        f"/api/v1/defect-cases/{case['id']}",
        json={
            "found_station_id": other_station_id,
            "possible_source_station_id": other_station_id,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["found_station_id"] == other_station_id
    assert body["possible_source_station_id"] == other_station_id
    assert "update" in _audit_actions(client, case["case_number"])


def test_editing_found_station_does_not_touch_cost(client, master_data):
    """Phase 0 finding, re-confirmed here: cost_per_drawer_at_time is a snapshot
    unrelated to station/category, and must not change on this edit."""
    case = _create_case(client, master_data).json()
    other_station_id = master_data["stations"]["Dado"]

    resp = client.patch(
        f"/api/v1/defect-cases/{case['id']}", json={"found_station_id": other_station_id}
    )
    assert resp.status_code == 200
    assert resp.json()["cost_per_drawer_at_time"] == case["cost_per_drawer_at_time"]


def test_editing_to_a_nonexistent_found_station_is_rejected(client, master_data):
    case = _create_case(client, master_data).json()
    resp = client.patch(f"/api/v1/defect-cases/{case['id']}", json={"found_station_id": 999999})
    assert resp.status_code == 404
    assert resp.json()["error"]["field"] == "found_station_id"


def test_editing_to_a_nonexistent_possible_source_station_is_rejected(client, master_data):
    case = _create_case(client, master_data).json()
    resp = client.patch(
        f"/api/v1/defect-cases/{case['id']}", json={"possible_source_station_id": 999999}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["field"] == "possible_source_station_id"


def test_can_assign_an_inactive_station_that_was_already_on_the_case(client, master_data):
    """An edit must never be blocked by the target station having since gone
    inactive - only the New Defect form's dropdown hides inactive stations from
    being offered as a NEW choice (Phase 1); the service layer only checks
    existence, same as at case creation."""
    station_id = master_data["stations"]["Dado"]
    client.patch(f"/api/v1/master-data/stations/{station_id}", json={"active": False})

    case = _create_case(client, master_data).json()
    resp = client.patch(f"/api/v1/defect-cases/{case['id']}", json={"found_station_id": station_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["found_station_id"] == station_id


# ---------------------------------------------------------------------------
# Defect items: add / edit / remove
# ---------------------------------------------------------------------------


def test_add_item_merges_into_existing_category(client, master_data):
    case = _create_case(client, master_data).json()
    category_id = master_data["categories"]["Sanding / Surface"]

    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/items",
        json={"defect_category_id": category_id, "affected_drawer_quantity": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sanding_items = [i for i in body["items"] if i["defect_category_id"] == category_id]
    assert len(sanding_items) == 1
    assert sanding_items[0]["affected_drawer_quantity"] == 3  # 1 (creation) + 2
    assert "item_add" in _audit_actions(client, case["case_number"])


def test_add_item_new_category(client, master_data):
    case = _create_case(client, master_data).json()
    third_category_id = master_data["categories"]["Assembly / Joint / Glue / Staple"]

    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/items",
        json={"defect_category_id": third_category_id, "affected_drawer_quantity": 1},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["items"]) == 3


def test_update_item_quantity_and_notes(client, master_data):
    case = _create_case(client, master_data).json()
    item_id = case["items"][0]["id"]

    resp = client.patch(
        f"/api/v1/defect-cases/{case['id']}/items/{item_id}",
        json={"affected_drawer_quantity": 5, "notes": "confirmed on recheck"},
    )
    assert resp.status_code == 200, resp.text
    updated = next(i for i in resp.json()["items"] if i["id"] == item_id)
    assert updated["affected_drawer_quantity"] == 5
    assert updated["notes"] == "confirmed on recheck"
    assert "item_update" in _audit_actions(client, case["case_number"])


def test_update_item_quantity_below_one_is_rejected(client, master_data):
    case = _create_case(client, master_data).json()
    item_id = case["items"][0]["id"]

    resp = client.patch(
        f"/api/v1/defect-cases/{case['id']}/items/{item_id}",
        json={"affected_drawer_quantity": 0},
    )
    assert resp.status_code in (400, 422)


def test_remove_item_succeeds_when_more_than_one_remains(client, master_data):
    case = _create_case(client, master_data).json()
    item_id = case["items"][0]["id"]

    resp = client.delete(f"/api/v1/defect-cases/{case['id']}/items/{item_id}")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["items"]) == 1
    assert "item_remove" in _audit_actions(client, case["case_number"])


def test_cannot_remove_the_last_item_on_a_case(client, master_data):
    case = _create_case(
        client,
        master_data,
        items=[
            {
                "defect_category_id": master_data["categories"]["Sanding / Surface"],
                "affected_drawer_quantity": 1,
            }
        ],
    ).json()
    item_id = case["items"][0]["id"]

    resp = client.delete(f"/api/v1/defect-cases/{case['id']}/items/{item_id}")
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "item_id"


def test_item_add_edit_remove_blocked_on_a_closed_case(client, master_data):
    case = _create_case(client, master_data).json()
    close_resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/status", json={"new_status": "Closed - Repaired"}
    )
    assert close_resp.status_code == 200, close_resp.text
    item_id = case["items"][0]["id"]
    category_id = master_data["categories"]["Assembly / Joint / Glue / Staple"]

    add_resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/items",
        json={"defect_category_id": category_id, "affected_drawer_quantity": 1},
    )
    assert add_resp.status_code == 400
    assert "reopen" in add_resp.json()["error"]["message"].lower()

    update_resp = client.patch(
        f"/api/v1/defect-cases/{case['id']}/items/{item_id}",
        json={"affected_drawer_quantity": 3},
    )
    assert update_resp.status_code == 400
    assert "reopen" in update_resp.json()["error"]["message"].lower()

    remove_resp = client.delete(f"/api/v1/defect-cases/{case['id']}/items/{item_id}")
    assert remove_resp.status_code == 400
    assert "reopen" in remove_resp.json()["error"]["message"].lower()


def test_reopen_edit_items_reclose_round_trip(client, master_data):
    """The documented escape hatch for the block above: reopen (already requires
    a note, already audited), edit items freely while Open, close again."""
    case = _create_case(client, master_data).json()
    case_id = case["id"]

    close_resp = client.post(
        f"/api/v1/defect-cases/{case_id}/status", json={"new_status": "Closed - Repaired"}
    )
    assert close_resp.status_code == 200, close_resp.text

    # Reopen requires a note (existing Phase 7 rule) - confirm that's still true
    # and not something this phase's item-edit gate bypasses.
    bare_reopen = client.post(f"/api/v1/defect-cases/{case_id}/status", json={"new_status": "Open"})
    assert bare_reopen.status_code == 400

    reopen_resp = client.post(
        f"/api/v1/defect-cases/{case_id}/status",
        json={"new_status": "Open", "note": "Found an additional defect on recheck"},
    )
    assert reopen_resp.status_code == 200, reopen_resp.text
    assert reopen_resp.json()["status"] == "Open"

    category_id = master_data["categories"]["Assembly / Joint / Glue / Staple"]
    add_resp = client.post(
        f"/api/v1/defect-cases/{case_id}/items",
        json={"defect_category_id": category_id, "affected_drawer_quantity": 1},
    )
    assert add_resp.status_code == 200, add_resp.text
    assert len(add_resp.json()["items"]) == 3

    item_id = case["items"][0]["id"]
    update_resp = client.patch(
        f"/api/v1/defect-cases/{case_id}/items/{item_id}",
        json={"notes": "re-inspected, still present"},
    )
    assert update_resp.status_code == 200, update_resp.text

    reclose_resp = client.post(
        f"/api/v1/defect-cases/{case_id}/status", json={"new_status": "Closed - Repaired"}
    )
    assert reclose_resp.status_code == 200, reclose_resp.text
    assert reclose_resp.json()["status"] == "Closed - Repaired"

    # And the gate is back in force now that it's closed again.
    remove_resp = client.delete(f"/api/v1/defect-cases/{case_id}/items/{item_id}")
    assert remove_resp.status_code == 400


# ---------------------------------------------------------------------------
# Photos: delete removes both the DB row and the file
# ---------------------------------------------------------------------------


def test_delete_photo_removes_db_row_and_file(client, master_data, tmp_path, monkeypatch):
    from app import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "uploads_dir", tmp_path)

    case = _create_case(client, master_data).json()
    upload_resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/photos",
        files={"file": ("drawer.jpg", io.BytesIO(b"\xff\xd8\xff" + b"0" * 1000), "image/jpeg")},
    )
    assert upload_resp.status_code == 200, upload_resp.text
    photo = upload_resp.json()
    saved_path = tmp_path / photo["stored_filename"]
    assert saved_path.exists()

    delete_resp = client.delete(f"/api/v1/defect-cases/{case['id']}/photos/{photo['id']}")
    assert delete_resp.status_code == 200, delete_resp.text
    assert delete_resp.json()["photos"] == []
    assert not saved_path.exists(), "photo file must be removed from disk, not just the DB row"

    session = client.testing_sessionmaker()
    try:
        assert session.get(DefectPhoto, photo["id"]) is None
    finally:
        session.close()

    assert "photo_delete" in _audit_actions(client, case["case_number"])


def test_delete_nonexistent_photo_is_a_clean_404(client, master_data):
    case = _create_case(client, master_data).json()
    resp = client.delete(f"/api/v1/defect-cases/{case['id']}/photos/999999")
    assert resp.status_code == 404


def test_delete_photo_against_a_real_file_backed_database(tmp_path, monkeypatch):
    """Extra coverage against a real file-backed SQLite engine (not the shared
    `client` fixture's :memory: + StaticPool one), for the same photo-delete path.

    Context, stated honestly: a live Playwright smoke test against a genuinely
    running uvicorn process (a real socket, the app's real database.py session
    config) hit sqlalchemy.orm.exc.ObjectDeletedError as a real HTTP 500 here -
    remove_photo used to return the ORM DefectPhoto instance itself AFTER
    deleting and committing it, and the router then read photo.id/
    original_filename/stored_filename off that same, now-deleted instance for
    the audit log. remove_photo now snapshots those fields into a plain dict
    BEFORE the delete (see app/services/defect_service.py) instead of returning
    the ORM instance, which is the actual fix.

    This test, built the same way with its own file-backed engine, does NOT
    reproduce that crash even with the pre-fix code restored (verified by hand
    while diagnosing this) - neither does the shared in-memory client fixture.
    Whatever process/threading condition triggers it live isn't recreated by
    TestClient's in-process ASGI transport. So this test is not a substitute
    for the live smoke test that actually caught the bug - it's kept as ordinary
    additional coverage against a real file, not as proof the regression can't
    recur."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.dependencies import get_db
    from app.main import app
    from app.models import DefectCategory, Station
    from app.seed_data import seed_master_data
    from app.services import auth_service

    db_path = tmp_path / "file_backed.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    seed_session = TestingSession()
    seed_master_data(seed_session)
    stations = {s.name: s.id for s in seed_session.query(Station).all()}
    categories = {c.name: c.id for c in seed_session.query(DefectCategory).all()}
    seed_session.close()

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    from app import config

    monkeypatch.setattr(config.get_settings(), "uploads_dir", tmp_path)

    test_client = TestClient(app)
    auth_db = TestingSession()
    token = auth_service.create_session(auth_db)
    auth_db.close()
    test_client.cookies.set(auth_service.SESSION_COOKIE_NAME, token)

    try:
        case = _create_case(
            test_client,
            {"stations": stations, "categories": categories},
        ).json()
        upload_resp = test_client.post(
            f"/api/v1/defect-cases/{case['id']}/photos",
            files={"file": ("drawer.jpg", io.BytesIO(b"\xff\xd8\xff" + b"0" * 1000), "image/jpeg")},
        )
        assert upload_resp.status_code == 200, upload_resp.text
        photo = upload_resp.json()

        delete_resp = test_client.delete(f"/api/v1/defect-cases/{case['id']}/photos/{photo['id']}")
        assert delete_resp.status_code == 200, delete_resp.text
        assert delete_resp.json()["photos"] == []
    finally:
        test_client.close()
        app.dependency_overrides.clear()
