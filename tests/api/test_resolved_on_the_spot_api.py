"""API tests for PROJECT_SPEC.md section 3.3: resolved-on-the-spot creation
(the default flow), Close Directly (the standard closing action from every
non-closed status), and the two KPIs they feed."""

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
# Resolved on the spot at entry - the default flow
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "disposition,expected_status",
    [
        ("Rework", "Closed - Repaired"),
        ("Scrap", "Closed - Scrapped"),
        ("Use As Is", "Closed - Use As Is"),
    ],
)
def test_create_resolved_on_the_spot_all_three_dispositions(
    client, master_data, disposition, expected_status
):
    """Scrap is now tucked behind "More options..." on the form, but it must
    still work end to end when explicitly chosen."""
    resp = _create_case(
        client,
        master_data,
        disposition=disposition,
        resolved_on_the_spot=True,
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
        disposition="Scrap",
        resolved_on_the_spot=True,
        repair_action="Other",
    )
    case = resp.json()

    from app.models import AuditLog

    session = client.testing_sessionmaker()
    entries = session.query(AuditLog).filter(AuditLog.action == "create").all()
    assert len(entries) == 1
    assert entries[0].entity_id == case["case_number"]
    assert '"resolved_on_the_spot": true' in entries[0].inputs_json
    session.close()


def test_create_resolved_on_the_spot_rejects_hold_disposition(client, master_data):
    resp = _create_case(
        client,
        master_data,
        disposition="Hold",
        resolved_on_the_spot=True,
        repair_action="Resanded",
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "resolved_on_the_spot"


def test_create_resolved_on_the_spot_requires_repair_action(client, master_data):
    resp = _create_case(client, master_data, disposition="Rework", resolved_on_the_spot=True)
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "repair_action"


# ---------------------------------------------------------------------------
# Leaving a case open - the secondary/exception path
# ---------------------------------------------------------------------------


def test_create_without_resolving_scrap_disposition_is_open_not_auto_closed(client, master_data):
    """Deliberate behavior change (PROJECT_SPEC.md section 3.2/3.3): Scrap no
    longer auto-closes at creation unless resolved_on_the_spot is explicitly set -
    this is what the "Not resolved yet - leave this case open" checkbox produces."""
    resp = _create_case(client, master_data, disposition="Scrap")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "Open"
    assert body["resolved_on_the_spot"] is False


def test_create_hold_disposition_is_left_open_as_waiting(client, master_data):
    """Hold is the other way to reach the "left open" state - it always saves as
    Waiting, no checkbox needed, since Hold means the disposition itself isn't
    decided yet."""
    resp = _create_case(client, master_data, disposition="Hold")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "Waiting"
    assert body["resolved_on_the_spot"] is False


def test_create_rework_left_open_enters_the_rework_queue(client, master_data):
    resp = _create_case(client, master_data, disposition="Rework")
    case = resp.json()
    assert case["status"] == "In Rework"

    queue = client.get("/api/v1/rework-queue").json()
    assert any(r["id"] == case["id"] for r in queue)


# ---------------------------------------------------------------------------
# Close Directly from the queue - the standard closing action, available from
# every non-closed status now, not a narrow "skip recheck" exception limited to
# In Rework.
# ---------------------------------------------------------------------------


def test_direct_close_statuses_appear_on_in_rework_case(client, master_data):
    case = _create_case(client, master_data, disposition="Rework").json()
    assert case["status"] == "In Rework"
    assert sorted(case["direct_close_statuses"]) == [
        "Closed - Repaired",
        "Closed - Scrapped",
        "Closed - Use As Is",
    ]

    queue = client.get("/api/v1/rework-queue").json()
    row = next(r for r in queue if r["id"] == case["id"])
    assert sorted(row["direct_close_statuses"]) == [
        "Closed - Repaired",
        "Closed - Scrapped",
        "Closed - Use As Is",
    ]


def test_direct_close_statuses_also_appear_on_open_case(client, master_data):
    """Direct close is available from every non-closed status now, including a
    freshly-created Open case that never entered the queue's In Rework state."""
    case = _create_case(client, master_data).json()
    assert case["status"] == "Open"
    assert sorted(case["direct_close_statuses"]) == [
        "Closed - Repaired",
        "Closed - Scrapped",
        "Closed - Use As Is",
    ]


@pytest.mark.parametrize(
    "target_status", ["Closed - Repaired", "Closed - Scrapped", "Closed - Use As Is"]
)
def test_direct_close_with_note_succeeds(client, master_data, target_status):
    case = _create_case(client, master_data, disposition="Rework").json()

    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": target_status, "note": "Confirmed fixed, no recheck needed."},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == target_status
    assert body["skipped_recheck"] is True

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


def test_legacy_recheck_path_still_works(client, master_data):
    """ "Ready for QC Recheck" is kept valid for backward compatibility - a case
    can still go through it and close from there without a note, same as every
    other direct close, and it correctly does NOT count as skipped."""
    case = _create_case(client, master_data, disposition="Rework").json()
    client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "Ready for QC Recheck"},
    )
    resp = client.post(
        f"/api/v1/defect-cases/{case['id']}/status",
        json={"new_status": "Closed - Repaired"},
    )
    assert resp.status_code == 200
    assert resp.json()["skipped_recheck"] is False


# ---------------------------------------------------------------------------
# KPIs: % Resolved On The Spot, % Queued Rework Closed Without Recheck
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


def test_pct_queued_rework_closed_without_recheck_kpi(client, master_data):
    skipped = _create_case(
        client, master_data, work_order_number="WO-KPI-SKIP", disposition="Rework"
    ).json()
    rechecked = _create_case(
        client, master_data, work_order_number="WO-KPI-RECHECK", disposition="Rework"
    ).json()

    client.post(
        f"/api/v1/defect-cases/{skipped['id']}/status",
        json={"new_status": "Closed - Repaired", "note": "Confirmed fixed."},
    )
    client.post(
        f"/api/v1/defect-cases/{rechecked['id']}/status",
        json={"new_status": "Ready for QC Recheck"},
    )
    client.post(
        f"/api/v1/defect-cases/{rechecked['id']}/status",
        json={"new_status": "Closed - Repaired", "note": "Recheck confirmed the repair held."},
    )

    resp = client.get(
        "/api/v1/reports/summary",
        params={"start_date": "2026-07-24", "end_date": "2026-07-24"},
    )
    body = resp.json()
    assert body["queued_rework_count"] == 2
    assert body["skipped_recheck_count"] == 1
    assert body["pct_queued_rework_closed_without_recheck"] == 50.0


def test_resolved_on_the_spot_case_excluded_from_queued_rework_denominator(client, master_data):
    """A case resolved on the spot never passes through In Rework, so it must not
    count toward the "% Queued Rework Closed Without Recheck" denominator."""
    _create_case(
        client,
        master_data,
        work_order_number="WO-KPI-INSTANT",
        disposition="Rework",
        resolved_on_the_spot=True,
        repair_action="Resanded",
    )
    resp = client.get(
        "/api/v1/reports/summary",
        params={"start_date": "2026-07-24", "end_date": "2026-07-24"},
    )
    body = resp.json()
    assert body["queued_rework_count"] == 0
    assert body["pct_queued_rework_closed_without_recheck"] is None
    assert body["resolved_on_the_spot_count"] == 1
    assert body["pct_resolved_on_the_spot"] == 100.0


def test_kpi_percentages_null_when_no_cases_in_period(client, master_data):
    resp = client.get(
        "/api/v1/reports/summary",
        params={"start_date": "2026-01-01", "end_date": "2026-01-01"},
    )
    body = resp.json()
    assert body["total_cases"] == 0
    assert body["pct_resolved_on_the_spot"] is None
    assert body["queued_rework_count"] == 0
    assert body["pct_queued_rework_closed_without_recheck"] is None
