"""2026-10 redesign, phase 6: the clamp that assembled a drawer.

Production count names it on every drawer event ("clamp", optional). A kickback
case takes the clamp of the event that opened it; it shows on the queue and in
the Reports Pareto by clamp.
"""

from __future__ import annotations

import pytest

from app.config import get_settings

KEY = "test-relay-key-do-not-use-in-prod"
EVENTS = "/api/v1/sync/drawer-events/ingest-raw"


@pytest.fixture()
def relay_key(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _event(
    event_id, type_="kickback", area="qc", unit=1, clamp="Clamp 2", at="2026-10-05T14:00:00+00:00"
):
    return {
        "event_id": event_id,
        "type": type_,
        "area": area,
        "order_no": "179459",
        "order_detail_id": 285016,
        "unit": unit,
        "occurred_at": at,
        "clamp": clamp,
    }


def _push(client, events):
    resp = client.post(
        EVENTS,
        json={"source": "eagle-drawers-production-count", "events": events},
        headers={"X-Relay-Key": KEY},
    )
    assert resp.status_code == 200, resp.text
    return resp


def test_a_kickback_case_shows_the_clamp_on_the_queue(client, relay_key):
    _push(client, [_event(1)])
    rows = client.get("/api/v1/rework-queue").json()
    assert [(r["kind"], r["clamp"]) for r in rows] == [("kickback", "Clamp 2")]


def test_events_without_a_clamp_are_still_accepted(client, relay_key):
    event = _event(1)
    del event["clamp"]
    _push(client, [event])
    assert client.get("/api/v1/rework-queue").json()[0]["clamp"] is None


def test_a_bad_clamp_value_is_ignored_not_refused(client, relay_key):
    _push(client, [_event(1, clamp=42)])
    assert client.get("/api/v1/rework-queue").json()[0]["clamp"] is None


def test_pareto_by_clamp_counts_cases_with_a_known_clamp(client, relay_key):
    _push(
        client,
        [
            _event(1, unit=1, clamp="Clamp 2"),
            _event(2, unit=2, clamp="Clamp 4"),
            _event(3, unit=3, clamp=None),
        ],
    )
    rows = client.get(
        "/api/v1/reports/pareto",
        params={"start_date": "2026-10-01", "end_date": "2026-10-31", "group_by": "clamp"},
    ).json()
    assert sorted((r["label"], r["defect_events"]) for r in rows) == [
        ("Clamp 2", 1),
        ("Clamp 4", 1),
    ]
