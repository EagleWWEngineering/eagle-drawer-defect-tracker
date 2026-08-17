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


def test_get_case_by_case_number(client, master_data):
    created = _create_case(client, master_data).json()
    resp = client.get(f"/api/v1/defect-cases/by-number/{created['case_number']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]

    missing = client.get("/api/v1/defect-cases/by-number/DF-20990101-9999")
    assert missing.status_code == 404


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
    # "Closed - Repaired" is now reachable directly from Open (PROJECT_SPEC.md
    # section 3.3 - Close Directly, requires a note instead of being rejected).
    # "Ready for QC Recheck" is not a direct-close target and not in
    # STATUS_TRANSITIONS for Open, so it's still a genuinely invalid transition.
    case = _create_case(client, master_data).json()
    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "Ready for QC Recheck"},
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


def test_bulk_delete_and_restore_defect_cases(client, master_data):
    case1 = _create_case(client, master_data, work_order_number="WO-BULK-1").json()
    case2 = _create_case(client, master_data, work_order_number="WO-BULK-2").json()
    case3 = _create_case(client, master_data, work_order_number="WO-BULK-3").json()

    resp = client.post("/api/v1/defect-cases/bulk-delete", json={"ids": [case1["id"], case2["id"]]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 2
    assert sorted(body["ids"]) == sorted([case1["id"], case2["id"]])

    assert client.get(f"/api/v1/defect-cases/{case1['id']}").status_code == 404
    assert client.get(f"/api/v1/defect-cases/{case2['id']}").status_code == 404
    assert client.get(f"/api/v1/defect-cases/{case3['id']}").status_code == 200

    include_resp = client.get(
        "/api/v1/defect-cases", params={"include_deleted": True, "page_size": 500}
    )
    deleted_ids = {c["id"] for c in include_resp.json()["cases"] if c["is_deleted"]}
    assert deleted_ids == {case1["id"], case2["id"]}

    restore_resp = client.post("/api/v1/defect-cases/bulk-restore", json={"ids": [case1["id"]]})
    assert restore_resp.status_code == 200
    restore_body = restore_resp.json()
    assert restore_body["count"] == 1
    assert restore_body["ids"] == [case1["id"]]

    assert client.get(f"/api/v1/defect-cases/{case1['id']}").status_code == 200
    assert client.get(f"/api/v1/defect-cases/{case2['id']}").status_code == 404


def test_bulk_delete_skips_unknown_and_already_deleted_ids(client, master_data):
    case = _create_case(client, master_data, work_order_number="WO-BULK-4").json()

    resp = client.post("/api/v1/defect-cases/bulk-delete", json={"ids": [case["id"], 999999]})
    assert resp.status_code == 200
    assert resp.json() == {"count": 1, "ids": [case["id"]]}

    # Deleting an already-deleted case again is a no-op, not an error.
    resp2 = client.post("/api/v1/defect-cases/bulk-delete", json={"ids": [case["id"]]})
    assert resp2.status_code == 200
    assert resp2.json() == {"count": 0, "ids": []}


def test_bulk_restore_skips_ids_that_are_not_currently_deleted(client, master_data):
    case = _create_case(client, master_data, work_order_number="WO-BULK-5").json()

    resp = client.post("/api/v1/defect-cases/bulk-restore", json={"ids": [case["id"]]})
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "ids": []}


def test_bulk_delete_rejects_empty_ids(client, master_data):
    resp = client.post("/api/v1/defect-cases/bulk-delete", json={"ids": []})
    assert resp.status_code == 422


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


def test_uploaded_photo_can_be_fetched_back(client, master_data):
    """Regression test for a 2026-08-17 incident: a freshly uploaded photo 404'd
    when its /uploads/... link was opened. The root cause was a live-deployment
    configuration gap (UPLOADS_DIR declared in render.yaml but never actually set
    in Render's dashboard for the already-existing service), not a save/serve path
    mismatch in the code - but this specific class of bug (upload "succeeds" yet
    the file can never be fetched back afterward) is exactly what this test guards
    against going forward, independent of any one deployment's configuration."""
    case = _create_case(client, master_data).json()
    tiny_png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "53de0000000c4944415478da6360000002000155a3c5330000000049454e44ae426082"
    )
    upload_resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/photos",
        files={"file": ("regression_test.png", io.BytesIO(tiny_png), "image/png")},
    )
    assert upload_resp.status_code == 200
    stored_filename = upload_resp.json()["stored_filename"]

    from app.config import get_settings

    settings = get_settings()
    saved_path = settings.uploads_dir / stored_filename
    try:
        assert saved_path.exists(), "upload reported success but the file isn't on disk"

        fetch_resp = client.get(f"/uploads/{stored_filename}")
        assert fetch_resp.status_code == 200
        assert fetch_resp.content == tiny_png
    finally:
        saved_path.unlink(missing_ok=True)


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


def test_recent_work_orders_returns_most_recently_used_first(client, master_data):
    _create_case(
        client, master_data, work_order_number="WO-OLD", detected_at="2026-07-24T08:00:00Z"
    )
    _create_case(
        client, master_data, work_order_number="WO-NEW", detected_at="2026-07-24T16:00:00Z"
    )

    resp = client.get("/api/v1/defect-cases/work-orders/recent")
    assert resp.status_code == 200
    work_orders = resp.json()
    assert work_orders.index("WO-NEW") < work_orders.index("WO-OLD")


def test_recent_work_orders_respects_limit(client, master_data):
    for i in range(5):
        _create_case(client, master_data, work_order_number=f"WO-LIMIT-{i}")
    resp = client.get("/api/v1/defect-cases/work-orders/recent", params={"limit": 2})
    assert len(resp.json()) == 2


def test_last_station_for_work_order_prefills_from_most_recent_case(client, master_data):
    _create_case(
        client,
        master_data,
        work_order_number="WO-STATION",
        found_station_id=master_data["stations"]["Dado"],
        detected_at="2026-07-24T08:00:00Z",
    )
    _create_case(
        client,
        master_data,
        work_order_number="WO-STATION",
        found_station_id=master_data["stations"]["Assembly"],
        detected_at="2026-07-24T16:00:00Z",
    )

    resp = client.get("/api/v1/defect-cases/work-orders/WO-STATION/last-station")
    assert resp.status_code == 200
    body = resp.json()
    assert body["found_station_name"] == "Assembly"


def test_last_station_for_unknown_work_order_returns_null(client, master_data):
    resp = client.get("/api/v1/defect-cases/work-orders/WO-NEVER-SEEN/last-station")
    assert resp.status_code == 200
    assert resp.json() is None


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
