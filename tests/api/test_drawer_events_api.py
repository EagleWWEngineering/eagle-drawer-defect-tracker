"""API tests for PROJECT_SPEC_PHASE11.md: UNDO-card kickbacks and auto-close.

- POST /api/v1/sync/drawer-events/ingest-raw (production count; X-Relay-Key)
  - "kickback" opens a Set Aside case for the drawer
  - "counted" (any station) closes the drawer's open cases as Repaired
- GET/PUT /api/v1/settings/undo-categories (Admin dropdowns)
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.models import DefectCase, DrawerEvent, SyncLog

INGEST_PATH = "/api/v1/sync/drawer-events/ingest-raw"
SETTINGS_PATH = "/api/v1/settings/undo-categories"
TEST_RELAY_KEY = "test-relay-key-do-not-use-in-prod"


@pytest.fixture()
def relay_key(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", TEST_RELAY_KEY)
    get_settings.cache_clear()
    yield TEST_RELAY_KEY
    get_settings.cache_clear()


def _event(event_id: int, type_: str = "kickback", **overrides) -> dict:
    event = {
        "event_id": event_id,
        "type": type_,
        "area": "qc",
        "order_no": "179459",
        "order_detail_id": 285016,
        "unit": 1,
        "occurred_at": "2026-10-01T14:42:07+00:00",
    }
    event.update(overrides)
    return event


def _push(client, events: list[dict], key: str | None = TEST_RELAY_KEY, **body):
    headers = {"X-Relay-Key": key} if key else {}
    payload = {"source": "eagle-drawers-production-count", "events": events, **body}
    return client.post(INGEST_PATH, json=payload, headers=headers)


def _cases(client) -> list[DefectCase]:
    db = client.testing_sessionmaker()
    try:
        return db.query(DefectCase).order_by(DefectCase.id).all()
    finally:
        db.close()


def _push_line(client, letter: str = "A") -> None:
    resp = client.post(
        "/api/v1/sync/order-lines/ingest-raw",
        json={"orders": {"179459": {"lines": [{"order_detail_id": 285016, "line": letter}]}}},
        headers={"X-Relay-Key": TEST_RELAY_KEY},
    )
    assert resp.status_code == 200, resp.text


# --- auth / validation ------------------------------------------------------


def test_missing_or_wrong_key_is_401(client, relay_key):
    assert _push(client, [_event(1)], key=None).status_code == 401
    assert _push(client, [_event(1)], key="wrong").status_code == 401
    assert client.post(INGEST_PATH, headers={"X-Relay-Key": "wrong"}).status_code == 401
    assert _cases(client) == []


def test_path_needs_no_login_session(client, relay_key):
    client.cookies.clear()
    assert _push(client, [_event(1)]).status_code == 200


@pytest.mark.parametrize(
    "bad",
    [
        {"event_id": 0},
        {"event_id": "7"},
        {"type": "undo"},
        {"area": "dado"},
        {"order_no": "17-94"},
        {"order_detail_id": None},
        {"unit": 0},
        {"occurred_at": "2026-10-01T14:42:07"},  # no timezone
        {"occurred_at": "yesterday"},
    ],
)
def test_one_bad_event_rejects_the_whole_request(client, relay_key, bad):
    resp = _push(client, [_event(1), {**_event(2), **bad}])
    assert resp.status_code == 422, resp.text
    assert _cases(client) == []
    db = client.testing_sessionmaker()
    try:
        assert db.query(DrawerEvent).count() == 0
    finally:
        db.close()


def test_body_must_have_an_events_list(client, relay_key):
    assert (
        client.post(INGEST_PATH, json={}, headers={"X-Relay-Key": TEST_RELAY_KEY}).status_code
        == 422
    )
    resp = client.post(
        INGEST_PATH, json={"events": "nope"}, headers={"X-Relay-Key": TEST_RELAY_KEY}
    )
    assert resp.status_code == 422


# --- kickback ---------------------------------------------------------------


def test_qc_kickback_opens_a_set_aside_case(client, relay_key, master_data):
    _push_line(client, "C")
    resp = _push(client, [_event(1)])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "received": 1,
        "duplicates": 0,
        "cases_created": 1,
        "cases_closed": 0,
        "failed": 0,
    }

    case = client.get("/api/v1/defect-cases").json()["cases"][0]
    assert case["status"] == "Open"
    assert case["disposition"] == "Set Aside"
    assert case["priority"] == "Normal"
    assert case["work_order_number"] == "179459"
    assert case["line_label"] == "C"
    assert (case["order_detail_id"], case["drawer_unit"]) == (285016, 1)
    assert case["entry_source"] == "undo_card"
    assert case["found_station_id"] == master_data["stations"]["QC / Sorting / Shipping"]
    assert [i["defect_category_id"] for i in case["items"]] == [master_data["categories"]["Other"]]
    assert [i["affected_drawer_quantity"] for i in case["items"]] == [1]
    assert case["production_date"] == "2026-10-01"
    assert "UNDO card at QC" in case["notes"]

    queue = client.get("/api/v1/rework-queue").json()
    assert len(queue) == 1
    assert queue[0]["entry_source"] == "undo_card"
    assert queue[0]["line_label"] == "C"
    assert queue[0]["disposition"] == "Set Aside"


def test_assembly_kickback_is_filed_under_assembly(client, relay_key, master_data):
    _push(client, [_event(1, area="assembly")])
    case = _cases(client)[0]
    assert case.found_station_id == master_data["stations"]["Assembly"]
    assert "UNDO card at Assembly In" in case.notes


def test_line_letter_left_blank_when_unknown_or_on_another_order(client, relay_key):
    _push(client, [_event(1)])
    assert _cases(client)[0].line_label is None

    _push_line(client, "A")  # line 285016 belongs to 179459
    _push(client, [_event(2, order_no="180000", order_detail_id=285016, unit=2)])
    assert _cases(client)[1].line_label is None


def test_second_kickback_for_an_open_drawer_makes_no_new_case(client, relay_key):
    _push(client, [_event(1)])
    resp = _push(client, [_event(2, occurred_at="2026-10-01T15:00:00+00:00")])
    assert resp.json()["cases_created"] == 0
    assert len(_cases(client)) == 1


def test_kickback_for_another_unit_is_its_own_case(client, relay_key):
    _push(client, [_event(1, unit=1), _event(2, unit=2)])
    assert [c.drawer_unit for c in _cases(client)] == [1, 2]


def test_admin_category_choice_is_used_per_area(client, relay_key, master_data):
    qc_kick = client.post(
        "/api/v1/master-data/defect-categories", json={"name": "QC Kick Back", "sort_order": 99}
    ).json()["id"]
    asm_kick = client.post(
        "/api/v1/master-data/defect-categories",
        json={"name": "Assembly Kick Back", "sort_order": 99},
    ).json()["id"]
    resp = client.put(
        SETTINGS_PATH, json={"qc_category_id": qc_kick, "assembly_category_id": asm_kick}
    )
    assert resp.status_code == 200, resp.text

    _push(client, [_event(1, area="qc", unit=1), _event(2, area="assembly", unit=2)])
    cases = _cases(client)
    db = client.testing_sessionmaker()
    try:
        items = [db.get(DefectCase, c.id).items[0].defect_category_id for c in cases]
    finally:
        db.close()
    assert items == [qc_kick, asm_kick]


def test_renamed_categories_still_work(client, relay_key, master_data):
    """The setting stores the id, and the default finds Other by seed_key -
    renaming either in Admin must not break kickbacks."""
    other = master_data["categories"]["Other"]
    client.patch(
        f"/api/v1/master-data/defect-categories/{other}", json={"name": "Other / Unclassified"}
    )
    _push(client, [_event(1)])
    db = client.testing_sessionmaker()
    try:
        assert db.get(DefectCase, _cases(client)[0].id).items[0].defect_category_id == other
    finally:
        db.close()


def test_undo_category_settings_default_and_validation(client):
    assert client.get(SETTINGS_PATH).json() == {
        "qc_category_id": None,
        "assembly_category_id": None,
    }
    resp = client.put(SETTINGS_PATH, json={"qc_category_id": 99999, "assembly_category_id": None})
    assert resp.status_code == 400
    assert client.get(SETTINGS_PATH).json()["qc_category_id"] is None


# --- counted / auto-close ---------------------------------------------------


def test_rescan_closes_the_kickback_case_as_repaired(client, relay_key):
    _push(client, [_event(1)])
    resp = _push(client, [_event(2, "counted", occurred_at="2026-10-01T16:05:00+00:00")])
    assert resp.json()["cases_closed"] == 1

    case = client.get(f"/api/v1/defect-cases/{_cases(client)[0].id}").json()
    assert case["status"] == "Closed - Repaired"
    assert case["status_history"][-1]["note"].startswith("Auto-closed: scanned at QC")
    assert client.get("/api/v1/rework-queue").json() == []


def test_rescan_at_any_station_closes_it(client, relay_key):
    _push(client, [_event(1, area="assembly")])
    _push(client, [_event(2, "counted", area="qc", occurred_at="2026-10-01T16:05:00+00:00")])
    assert _cases(client)[0].status == "Closed - Repaired"


def test_count_from_before_the_kickback_does_not_close_it(client, relay_key):
    """QC scans first (+1), then UNDOes it. If that earlier +1 arrives late,
    it must not close the case it came before."""
    _push(client, [_event(2)])
    _push(client, [_event(1, "counted", occurred_at="2026-10-01T14:40:00+00:00")])
    assert _cases(client)[0].status == "Open"


def test_kickback_and_rescan_in_one_batch(client, relay_key):
    resp = _push(
        client,
        [
            _event(2, "counted", occurred_at="2026-10-01T16:05:00+00:00"),
            _event(1, "kickback"),
        ],
    )
    assert resp.json()["cases_created"] == 1
    assert resp.json()["cases_closed"] == 1
    assert _cases(client)[0].status == "Closed - Repaired"


def test_count_closes_a_phone_logged_case_for_that_drawer(client, relay_key, master_data):
    created = client.post(
        "/api/v1/defect-cases",
        json={
            "production_date": "2026-10-01",
            "detected_at": "2026-10-01T13:00:00Z",
            "work_order_number": "179459",
            "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
            "priority": "Normal",
            "disposition": "Set Aside",
            "items": [{"defect_category_id": master_data["categories"]["Sanding / Surface"]}],
            "order_detail_id": 285016,
            "drawer_unit": 1,
        },
    ).json()
    _push(client, [_event(1, "counted")])
    assert (
        client.get(f"/api/v1/defect-cases/{created['id']}").json()["status"] == "Closed - Repaired"
    )


def test_count_for_other_drawers_or_cases_without_id_closes_nothing(client, relay_key, master_data):
    client.post(
        "/api/v1/defect-cases",
        json={
            "production_date": "2026-10-01",
            "detected_at": "2026-10-01T13:00:00Z",
            "work_order_number": "179459",
            "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
            "priority": "Normal",
            "disposition": "Set Aside",
            "items": [{"defect_category_id": master_data["categories"]["Sanding / Surface"]}],
        },
    )
    _push(client, [_event(1, unit=2)])
    resp = _push(client, [_event(2, "counted", unit=1, occurred_at="2026-10-01T16:00:00+00:00")])
    assert resp.json()["cases_closed"] == 0
    assert [c.status for c in _cases(client)] == ["Open", "Open"]


def test_manual_close_still_works_and_count_leaves_closed_cases_alone(client, relay_key):
    _push(client, [_event(1)])
    case_id = _cases(client)[0].id
    resp = client.post(
        f"/api/v1/defect-cases/{case_id}/status", json={"new_status": "Closed - Use As Is"}
    )
    assert resp.status_code == 200, resp.text
    _push(client, [_event(2, "counted", occurred_at="2026-10-01T16:00:00+00:00")])
    assert _cases(client)[0].status == "Closed - Use As Is"


# --- resends / logging -------------------------------------------------------


def test_resent_events_are_applied_once(client, relay_key):
    _push(client, [_event(1)])
    _push(client, [_event(2, "counted", occurred_at="2026-10-01T16:00:00+00:00")])
    # The kickback arrives again after its case closed - must not reopen a new one.
    resp = _push(client, [_event(1), _event(2, "counted", occurred_at="2026-10-01T16:00:00+00:00")])
    assert resp.json() == {
        "received": 2,
        "duplicates": 2,
        "cases_created": 0,
        "cases_closed": 0,
        "failed": 0,
    }
    assert len(_cases(client)) == 1


def test_every_event_is_kept_with_its_outcome(client, relay_key):
    _push(client, [_event(1), _event(2, "counted", unit=5)])
    db = client.testing_sessionmaker()
    try:
        outcomes = {e.event_id: e.outcome for e in db.query(DrawerEvent).all()}
    finally:
        db.close()
    assert outcomes[1].startswith("case created: ")
    assert outcomes[2] == "no open case"


def test_sync_log_only_for_pushes_that_changed_something(client, relay_key):
    _push(client, [_event(1, "counted")])
    _push(client, [_event(2)])
    db = client.testing_sessionmaker()
    try:
        logs = db.query(SyncLog).all()
    finally:
        db.close()
    assert len(logs) == 1
    assert logs[0].source_url.endswith("/drawer-events")
    assert logs[0].records_created == 1
