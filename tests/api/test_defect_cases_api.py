"""API tests: create/retrieve, filters, status transitions + audit, soft delete, photos."""

from __future__ import annotations

import io


def _create_case(client, master_data, **overrides):
    payload = {
        "production_date": "2026-07-24",
        "detected_at": "2026-07-24T14:30:00Z",
        "work_order_number": "WO-5001",
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


def test_create_case_with_multiple_categories_and_retrieve(client, master_data):
    resp = _create_case(client, master_data)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["case_number"].startswith("DF-20260724-")
    assert len(body["items"]) == 2
    assert body["defect_event_count"] == 2
    assert body["status"] == "Open"

    get_resp = client.get(f"/api/v1/defect-cases/{body['id']}")
    assert get_resp.status_code == 200
    assert get_resp.json()["case_number"] == body["case_number"]


def test_possible_source_station_is_never_labeled_root_cause(client, master_data):
    resp = _create_case(
        client,
        master_data,
        possible_source_station_id=master_data["stations"]["Dado"],
    )
    body = resp.json()
    assert body["possible_source_station_name"] == "Dado"
    # The field name itself must say "possible_source", never "root_cause".
    assert "root_cause" in body
    assert body["root_cause"] is None


def test_filter_by_every_supported_dimension(client, master_data):
    c1 = _create_case(client, master_data, work_order_number="WO-FILTER-A").json()
    _create_case(
        client,
        master_data,
        work_order_number="WO-FILTER-B",
        priority="Urgent",
        found_station_id=master_data["stations"]["Dado"],
    )

    def count(**params):
        return client.get("/api/v1/defect-cases", params=params).json()["total"]

    assert count(work_order_number="WO-FILTER-A") == 1
    assert count(priority="Urgent") == 1
    assert count(found_station_id=master_data["stations"]["Dado"]) == 1
    assert count(category_id=master_data["categories"]["Sanding / Surface"]) == 2
    assert count(status="Open") == 2
    assert count(start_date="2026-07-24", end_date="2026-07-24") == 2
    assert count(disposition="Rework") == 0

    client.post(f"/api/v1/defect-cases/{c1['id']}/status", json={"new_status": "In Rework"})
    assert count(status="In Rework") == 1


def test_update_status_writes_status_history_and_audit(client, master_data):
    case = _create_case(client, master_data).json()

    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "In Rework", "note": "Starting rework"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "In Rework"
    assert body["status_history"][-1]["to_status"] == "In Rework"
    assert body["status_history"][-1]["note"] == "Starting rework"

    from app.models import AuditLog

    session = client.testing_sessionmaker()
    entries = session.query(AuditLog).filter(AuditLog.action == "status_change").all()
    assert len(entries) == 1
    assert entries[0].entity_id == case["case_number"]
    session.close()


def test_invalid_transition_returns_400_with_field_error(client, master_data):
    case = _create_case(client, master_data).json()
    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "Closed - Repaired"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "new_status"


def test_soft_delete_hides_from_normal_queries_but_preserves_record(client, master_data):
    case = _create_case(client, master_data).json()

    del_resp = client.delete(f"/api/v1/defect-cases/{case['id']}")
    assert del_resp.status_code == 200

    list_resp = client.get("/api/v1/defect-cases", params={"work_order_number": "WO-5001"})
    assert list_resp.json()["total"] == 0

    get_resp = client.get(f"/api/v1/defect-cases/{case['id']}")
    assert get_resp.status_code == 404

    include_resp = client.get(
        "/api/v1/defect-cases", params={"work_order_number": "WO-5001", "include_deleted": True}
    )
    assert include_resp.json()["total"] == 1


def test_duplicate_category_in_same_request_merges_not_double_counted(client, master_data):
    payload = {
        "production_date": "2026-07-24",
        "detected_at": "2026-07-24T14:30:00Z",
        "work_order_number": "WO-DUPLICATE",
        "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
        "priority": "Normal",
        "items": [
            {
                "defect_category_id": master_data["categories"]["Sanding / Surface"],
                "affected_drawer_quantity": 1,
            },
            {
                "defect_category_id": master_data["categories"]["Sanding / Surface"],
                "affected_drawer_quantity": 2,
            },
        ],
    }
    resp = client.post("/api/v1/defect-cases", json=payload)
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["affected_drawer_quantity"] == 3


def test_oversized_photo_upload_is_rejected(client, master_data):
    case = _create_case(client, master_data).json()
    big_bytes = b"\xff\xd8\xff" + b"0" * (9 * 1024 * 1024)  # ~9 MB, over the 8 MB default limit
    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/photos",
        files={"file": ("big.jpg", io.BytesIO(big_bytes), "image/jpeg")},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "file"


def test_unsafe_file_type_upload_is_rejected(client, master_data):
    case = _create_case(client, master_data).json()
    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/photos",
        files={"file": ("evil.exe", io.BytesIO(b"MZ..."), "application/x-msdownload")},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "file"


def test_valid_photo_upload_succeeds(client, master_data, tmp_path, monkeypatch):
    from app import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "uploads_dir", tmp_path)

    case = _create_case(client, master_data).json()
    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/photos",
        files={"file": ("drawer.jpg", io.BytesIO(b"\xff\xd8\xff" + b"0" * 1000), "image/jpeg")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["original_filename"] == "drawer.jpg"
