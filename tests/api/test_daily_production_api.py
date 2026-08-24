"""API tests for the Daily Production Summary endpoint."""

from __future__ import annotations


def test_upsert_and_read_back(client):
    resp = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 100,
            "drawers_rejected_unique": 10,
            "drawers_reworked": 7,
            "drawers_scrapped": 2,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["drawers_inspected"] == 100
    assert body["warnings"] == []

    list_resp = client.get("/api/v1/daily-production")
    assert len(list_resp.json()) == 1


def test_hard_rule_rejected_gt_inspected_returns_400(client):
    resp = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 5,
            "drawers_rejected_unique": 6,
            "drawers_reworked": 0,
            "drawers_scrapped": 0,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "drawers_rejected_unique"


def test_soft_warning_without_note_is_rejected_with_note_is_allowed(client):
    resp = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 50,
            "drawers_rejected_unique": 0,
            "drawers_reworked": 3,
            "drawers_scrapped": 0,
        },
    )
    assert resp.status_code == 400

    resp2 = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 50,
            "drawers_rejected_unique": 0,
            "drawers_reworked": 3,
            "drawers_scrapped": 0,
            "notes": "Reworked yesterday's rejects.",
        },
    )
    assert resp2.status_code == 200
    assert len(resp2.json()["warnings"]) >= 1


def _create_case(client, master_data, wo, disposition=None, resolved_on_the_spot=False):
    payload = {
        "production_date": "2026-07-24",
        "detected_at": "2026-07-24T14:30:00Z",
        "work_order_number": wo,
        "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
        "priority": "Normal",
        "items": [
            {
                "defect_category_id": master_data["categories"]["Sanding / Surface"],
                "affected_drawer_quantity": 1,
            }
        ],
    }
    if disposition:
        payload["disposition"] = disposition
    if resolved_on_the_spot:
        payload["resolved_on_the_spot"] = True
        payload["repair_action"] = "Resanded"
    return client.post("/api/v1/defect-cases", json=payload).json()


def test_suggested_counts_endpoint_reflects_real_defect_cases(client, master_data):
    _create_case(client, master_data, "WO-SUGGEST-1")
    _create_case(
        client, master_data, "WO-SUGGEST-2", disposition="Rework", resolved_on_the_spot=True
    )

    resp = client.get("/api/v1/daily-production/2026-07-24/suggested-counts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["suggested_drawers_rejected_unique"] == 2
    assert body["defect_case_count"] == 2
    # PROJECT_SPEC_PHASE7.md: no more suggested_drawers_reworked field - Rework
    # Rate is computed straight from cases, not a suggested/typed number.
    assert "suggested_drawers_reworked" not in body


def test_suggested_counts_endpoint_never_writes_a_summary_row(client, master_data):
    """Purely a read - calling it must never create/modify a DailyProductionSummary,
    so it can never silently overwrite an already-saved entry."""
    _create_case(client, master_data, "WO-SUGGEST-3")
    client.get("/api/v1/daily-production/2026-07-24/suggested-counts")

    rows = client.get("/api/v1/daily-production").json()
    assert rows == []


def test_manual_override_of_a_saved_field_persists_and_is_not_overwritten(client, master_data):
    """Saving a Daily Summary with a value that differs from what the defect cases
    would suggest must persist exactly what was submitted - the suggestion is only
    ever a pre-fill, never enforced server-side."""
    _create_case(client, master_data, "WO-OVERRIDE-1")
    _create_case(client, master_data, "WO-OVERRIDE-2")

    # The suggestion would be 2 rejected, but staff knows better (e.g. one was a
    # duplicate paper-log entry) and overrides it to 1.
    resp = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 10,
            "drawers_rejected_unique": 1,
            "drawers_reworked": 0,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["drawers_rejected_unique"] == 1

    # Recalculating afterward must not touch the saved row.
    client.get("/api/v1/daily-production/2026-07-24/suggested-counts")
    rows = client.get("/api/v1/daily-production").json()
    assert rows[0]["drawers_rejected_unique"] == 1


def test_recalculate_endpoint_reflects_new_cases_logged_after_first_load(client, master_data):
    _create_case(client, master_data, "WO-RECALC-1")
    first = client.get("/api/v1/daily-production/2026-07-24/suggested-counts").json()
    assert first["suggested_drawers_rejected_unique"] == 1

    _create_case(client, master_data, "WO-RECALC-2")
    second = client.get("/api/v1/daily-production/2026-07-24/suggested-counts").json()
    assert second["suggested_drawers_rejected_unique"] == 2


def test_upsert_without_scrapped_field_defaults_new_row_to_zero(client):
    resp = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 10,
            "drawers_rejected_unique": 1,
            "drawers_reworked": 0,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["drawers_scrapped"] == 0


def test_upsert_without_scrapped_field_preserves_existing_value(client):
    client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 100,
            "drawers_rejected_unique": 10,
            "drawers_reworked": 5,
            "drawers_scrapped": 3,
        },
    )
    resp = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 120,
            "drawers_rejected_unique": 12,
            "drawers_reworked": 6,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["drawers_scrapped"] == 3


def test_same_date_and_shift_upserts_not_duplicates(client):
    client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 100,
            "drawers_rejected_unique": 10,
            "drawers_reworked": 5,
            "drawers_scrapped": 2,
        },
    )
    client.put(
        "/api/v1/daily-production/2026-07-24",
        json={
            "shift": "Day",
            "drawers_inspected": 120,
            "drawers_rejected_unique": 12,
            "drawers_reworked": 6,
            "drawers_scrapped": 3,
        },
    )
    rows = client.get("/api/v1/daily-production").json()
    assert len(rows) == 1
    assert rows[0]["drawers_inspected"] == 120


def test_reworked_case_count_appears_on_upsert_and_list(client, master_data):
    """PROJECT_SPEC_PHASE7.md: read-only, case-derived - not the (now optional)
    drawers_reworked field on the payload."""
    resp = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={"shift": "Day", "drawers_inspected": 10, "drawers_rejected_unique": 1},
    )
    assert resp.json()["reworked_case_count"] == 0

    client.post(
        "/api/v1/defect-cases",
        json={
            "production_date": "2026-07-24",
            "detected_at": "2026-07-24T14:30:00Z",
            "work_order_number": "WO-REWORKED-COL",
            "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
            "priority": "Normal",
            "items": [{"defect_category_id": master_data["categories"]["Sanding / Surface"]}],
            "disposition": "Rework",
        },
    )

    resp = client.put(
        "/api/v1/daily-production/2026-07-24",
        json={"shift": "Day", "drawers_inspected": 10, "drawers_rejected_unique": 1},
    )
    assert resp.json()["reworked_case_count"] == 1

    rows = client.get("/api/v1/daily-production").json()
    assert rows[0]["reworked_case_count"] == 1
