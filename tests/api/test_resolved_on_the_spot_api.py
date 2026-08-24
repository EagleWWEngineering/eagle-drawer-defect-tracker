"""API tests for PROJECT_SPEC_PHASE7.md: resolved-on-the-spot creation (the
default flow for Rework, with an outcome choice), Close Directly (the standard
closing action from every non-closed status), and the % Resolved On The Spot
KPI it feeds."""

from __future__ import annotations

import pytest


def _create_case(client, master_data, **overrides):
    payload = {
        "production_date": "2026-07-24",
        "detected_at": "2026-07-24T14:30:00Z",
        "work_order_number": "WO-6001",
        "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
        "priority": "Normal",
        "items": [
            {
                "defect_category_id": master_data["categories"]["Sanding / Surface"],
                "affected_drawer_quantity": 1,
            }
        ],
    }
    payload.update(overrides)
    return client.post("/api/v1/defect-cases", json=payload)


# ---------------------------------------------------------------------------
# Resolved on the spot at entry - the default flow, Rework disposition only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome,expected_status",
    [
        (None, "Closed - Repaired"),  # default outcome when omitted
        ("Repaired", "Closed - Repaired"),
        ("Use As Is", "Closed - Use As Is"),
    ],
)
def test_create_resolved_on_the_spot_rework_outcomes(client, master_data, outcome, expected_status):
    resp = _create_case(
        client,
        master_data,
        disposition="Rework",
        resolved_on_the_spot=True,
        instant_close_outcome=outcome,
        repair_action="Resanded",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == expected_status
    assert body["resolved_on_the_spot"] is True
    assert body["closed_at"] is not None
    assert body["status_history"][0]["note"] == "Resolved on the spot at entry"


def test_create_resolved_on_the_spot_writes_create_audit_entry(client, master_data):
    resp = _create_case(
        client,
        master_data,
        disposition="Rework",
        resolved_on_the_spot=True,
        instant_close_outcome="Use As Is",
        repair_action="Buyer accepted as-is",
    )
    case = resp.json()

    from app.models import AuditLog

    session = client.testing_sessionmaker()
    entries = session.query(AuditLog).filter(AuditLog.action == "create").all()
    assert len(entries) == 1
    assert entries[0].entity_id == case["case_number"]
    assert '"resolved_on_the_spot": true' in entries[0].inputs_json
    session.close()


def test_create_resolved_on_the_spot_rejects_set_aside_disposition(client, master_data):
    resp = _create_case(
        client,
        master_data,
        disposition="Set Aside",
        resolved_on_the_spot=True,
        repair_action="Resanded",
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "resolved_on_the_spot"


def test_create_resolved_on_the_spot_requires_repair_action(client, master_data):
    resp = _create_case(client, master_data, disposition="Rework", resolved_on_the_spot=True)
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "repair_action"


def test_create_rejects_retired_disposition(client, master_data):
    """Retired for new entry (PROJECT_SPEC_PHASE7.md) - Use As Is/Hold/Scrap are
    no longer legal dispositions to CREATE a case with, even without
    resolved_on_the_spot."""
    resp = _create_case(client, master_data, disposition="Use As Is")
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "disposition"


def test_create_rejects_invalid_instant_close_outcome(client, master_data):
    resp = _create_case(
        client,
        master_data,
        disposition="Rework",
        resolved_on_the_spot=True,
        instant_close_outcome="Scrapped",
        repair_action="Resanded",
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "instant_close_outcome"


# ---------------------------------------------------------------------------
# Leaving a case open - the secondary/exception path. Every non-instant case
# lands on Open now, regardless of disposition.
# ---------------------------------------------------------------------------


def test_create_set_aside_is_always_open(client, master_data):
    resp = _create_case(client, master_data, disposition="Set Aside")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "Open"
    assert body["resolved_on_the_spot"] is False


def test_create_rework_left_open_enters_the_rework_queue(client, master_data):
    resp = _create_case(client, master_data, disposition="Rework")
    case = resp.json()
    assert case["status"] == "Open"

    queue = client.get("/api/v1/rework-queue").json()
    assert any(r["id"] == case["id"] for r in queue)


# ---------------------------------------------------------------------------
# Close Directly from the queue - the standard closing action, available from
# every non-closed status.
# ---------------------------------------------------------------------------


def test_direct_close_statuses_appear_on_open_case(client, master_data):
    case = _create_case(client, master_data, disposition="Rework").json()
    assert case["status"] == "Open"
    assert sorted(case["direct_close_statuses"]) == ["Closed - Repaired", "Closed - Use As Is"]

    queue = client.get("/api/v1/rework-queue").json()
    row = next(r for r in queue if r["id"] == case["id"])
    assert sorted(row["direct_close_statuses"]) == ["Closed - Repaired", "Closed - Use As Is"]


@pytest.mark.parametrize("target_status", ["Closed - Repaired", "Closed - Use As Is"])
def test_direct_close_with_note_succeeds(client, master_data, target_status):
    case = _create_case(client, master_data, disposition="Rework").json()

    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": target_status, "note": "Confirmed fixed, no recheck needed."},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == target_status

    # Direct close no longer offered once the case is closed.
    assert body["direct_close_statuses"] == []


def test_direct_close_without_note_succeeds(client, master_data):
    """The note is optional supplementary detail for a normal closure - the
    repair-action preset is the primary structured record of what was done."""
    case = _create_case(client, master_data, disposition="Rework").json()
    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "Closed - Repaired"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "Closed - Repaired"
    assert body["status_history"][-1]["note"] is None


def test_direct_close_with_blank_note_succeeds(client, master_data):
    case = _create_case(client, master_data, disposition="Rework").json()
    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "Closed - Repaired", "note": "   "},
    )
    assert resp.status_code == 200, resp.text


def test_direct_close_rejects_retired_target_status(client, master_data):
    case = _create_case(client, master_data, disposition="Rework").json()
    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "Closed - Scrapped", "note": "No longer a valid target."},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "new_status"


def test_reopen_a_closed_case_still_requires_a_note(client, master_data):
    """Reopening is the one transition NOT relaxed by the note-optional change."""
    case = _create_case(client, master_data, disposition="Rework").json()
    client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "Closed - Repaired"},
    )
    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "Open"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "note"

    resp2 = client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "Open", "note": "QC recheck found the repair failed."},
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "Open"


# ---------------------------------------------------------------------------
# % Resolved On The Spot KPI - kept, definition unchanged by Phase 7.
# ---------------------------------------------------------------------------


def test_pct_resolved_on_the_spot_kpi(client, master_data):
    _create_case(
        client,
        master_data,
        work_order_number="WO-KPI-1",
        disposition="Rework",
        resolved_on_the_spot=True,
        repair_action="Resanded",
    )
    _create_case(client, master_data, work_order_number="WO-KPI-2")  # normal Open case

    resp = client.get(
        "/api/v1/reports/summary",
        params={"start_date": "2026-07-24", "end_date": "2026-07-24"},
    )
    body = resp.json()
    assert body["total_cases"] == 2
    assert body["resolved_on_the_spot_count"] == 1
    assert body["pct_resolved_on_the_spot"] == 50.0


def test_kpi_percentages_null_when_no_cases_in_period(client, master_data):
    resp = client.get(
        "/api/v1/reports/summary",
        params={"start_date": "2026-01-01", "end_date": "2026-01-01"},
    )
    body = resp.json()
    assert body["total_cases"] == 0
    assert body["pct_resolved_on_the_spot"] is None


def test_queued_rework_kpis_no_longer_exist(client, master_data):
    """PROJECT_SPEC_PHASE7.md: removed entirely - no recheck status exists."""
    resp = client.get(
        "/api/v1/reports/summary",
        params={"start_date": "2026-07-24", "end_date": "2026-07-24"},
    )
    body = resp.json()
    assert "queued_rework_count" not in body
    assert "skipped_recheck_count" not in body
    assert "pct_queued_rework_closed_without_recheck" not in body
    assert "cost_basis" not in body
    assert "defect_case_rework_count" not in body


def test_legacy_recheck_path_still_closeable_without_note(client, master_data):
    """ "Ready for QC Recheck" is retired for new entry, but a case that somehow
    still carries it (seeded directly here to simulate a stray pre-migration
    row) must still be closeable, same as any other non-closed status."""
    case = _create_case(client, master_data, disposition="Rework").json()

    from app.models import DefectCase

    session = client.testing_sessionmaker()
    db_case = session.get(DefectCase, case["id"])
    db_case.status = "Ready for QC Recheck"
    session.commit()
    session.close()

    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "Closed - Repaired"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "Closed - Repaired"
