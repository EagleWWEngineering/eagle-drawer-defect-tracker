"""API tests for the Phase 6 daily schedule endpoints:
  GET/PUT /api/v1/daily-production/schedule
  GET /api/v1/daily-production/schedule-attainment
  GET /api/v1/reports/date-preset

See docs/PRODUCTION_BRIEF_SCHEDULE_SOURCE.md and the PROJECT_SPEC.md Phase 6
addendum.
"""

from __future__ import annotations

import datetime as dt

# ---------------------------------------------------------------------------
# GET/PUT /api/v1/daily-production/schedule
# ---------------------------------------------------------------------------


def test_put_schedule_creates_a_manual_row(client):
    resp = client.put(
        "/api/v1/daily-production/schedule",
        json={"production_date": "2026-08-20", "drawers_scheduled": 400},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["drawers_scheduled"] == 400
    assert body["source"] == "manual"
    assert body["is_synced"] is False


def test_get_schedule_by_date_is_empty_list_when_unknown(client):
    resp = client.get("/api/v1/daily-production/schedule", params={"date": "2026-08-20"})
    assert resp.status_code == 200
    assert resp.json() == {"schedules": []}


def test_get_schedule_by_date_returns_the_saved_row(client):
    client.put(
        "/api/v1/daily-production/schedule",
        json={"production_date": "2026-08-20", "drawers_scheduled": 400},
    )
    resp = client.get("/api/v1/daily-production/schedule", params={"date": "2026-08-20"})
    assert resp.status_code == 200
    schedules = resp.json()["schedules"]
    assert len(schedules) == 1
    assert schedules[0]["drawers_scheduled"] == 400


def test_get_schedule_range_omits_dates_with_no_row(client):
    client.put(
        "/api/v1/daily-production/schedule",
        json={"production_date": "2026-08-20", "drawers_scheduled": 400},
    )
    resp = client.get(
        "/api/v1/daily-production/schedule",
        params={"start_date": "2026-08-19", "end_date": "2026-08-21"},
    )
    assert resp.status_code == 200
    schedules = resp.json()["schedules"]
    assert [s["production_date"] for s in schedules] == ["2026-08-20"]


def test_get_schedule_requires_date_or_range(client):
    resp = client.get("/api/v1/daily-production/schedule")
    assert resp.status_code == 400


def test_put_schedule_negative_count_returns_400(client):
    resp = client.put(
        "/api/v1/daily-production/schedule",
        json={"production_date": "2026-08-20", "drawers_scheduled": -1},
    )
    assert resp.status_code == 422  # Pydantic Field(ge=0) rejects it first


# ---------------------------------------------------------------------------
# GET /api/v1/daily-production/schedule-attainment
# ---------------------------------------------------------------------------


def test_schedule_attainment_two_shift_day_sums_before_comparing_to_schedule(client):
    client.put(
        "/api/v1/daily-production/schedule",
        json={"production_date": "2026-08-20", "drawers_scheduled": 400},
    )
    client.put(
        "/api/v1/daily-production/2026-08-20",
        json={
            "shift": "Day",
            "drawers_inspected": 150,
            "drawers_rejected_unique": 0,
            "drawers_reworked": 0,
        },
    )
    client.put(
        "/api/v1/daily-production/2026-08-20",
        json={
            "shift": "Night",
            "drawers_inspected": 100,
            "drawers_rejected_unique": 0,
            "drawers_reworked": 0,
        },
    )

    resp = client.get(
        "/api/v1/daily-production/schedule-attainment",
        params={"start_date": "2026-08-20", "end_date": "2026-08-20"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_scheduled"] == 400
    # 150 + 100, not double-counted, not just one shift's worth.
    assert body["total_inspected"] == 250
    assert body["attainment_pct"] == 62.5
    assert len(body["days"]) == 1
    assert body["days"][0]["drawers_scheduled"] == 400
    assert body["days"][0]["drawers_inspected"] == 250


def test_schedule_attainment_zero_scheduled_with_nothing_inspected_is_a_holiday(client):
    """Working Days Logic (Part C addendum): scheduled 0 + nothing inspected on a
    weekday is a holiday, not a working day - it's flagged is_working_day=False
    and excluded from the totals entirely (None, not a real 0), same as any
    other day with no daily_schedules row at all."""
    client.put(
        "/api/v1/daily-production/schedule",
        json={"production_date": "2026-08-20", "drawers_scheduled": 0},
    )
    resp = client.get(
        "/api/v1/daily-production/schedule-attainment",
        params={"start_date": "2026-08-20", "end_date": "2026-08-20"},
    )
    body = resp.json()
    assert body["total_scheduled"] is None
    assert body["attainment_pct"] is None
    assert body["days"][0]["is_working_day"] is False


def test_schedule_attainment_zero_scheduled_with_inspections_is_a_real_zero(client):
    """Same scheduled-0 entry, but drawers WERE inspected that day - a working
    day after all (not a holiday), so the real 0 counts in total_scheduled."""
    client.put(
        "/api/v1/daily-production/schedule",
        json={"production_date": "2026-08-20", "drawers_scheduled": 0},
    )
    client.put(
        "/api/v1/daily-production/2026-08-20",
        json={
            "shift": "Day",
            "drawers_inspected": 12,
            "drawers_rejected_unique": 0,
            "drawers_reworked": 0,
        },
    )
    resp = client.get(
        "/api/v1/daily-production/schedule-attainment",
        params={"start_date": "2026-08-20", "end_date": "2026-08-20"},
    )
    body = resp.json()
    assert body["total_scheduled"] == 0
    assert body["attainment_pct"] is None  # 0 scheduled -> still N/A, not a divide-by-zero
    assert body["days"][0]["is_working_day"] is True


def test_schedule_attainment_unknown_schedule_is_na(client):
    resp = client.get(
        "/api/v1/daily-production/schedule-attainment",
        params={"start_date": "2026-08-20", "end_date": "2026-08-20"},
    )
    body = resp.json()
    assert body["total_scheduled"] is None
    assert body["attainment_pct"] is None
    assert body["days"][0]["drawers_scheduled"] is None


def test_schedule_attainment_gap_dates(client):
    """A scheduled date with no summary row shows a zero-completed day; a summary
    row with no schedule shows an unknown (None), not zero, scheduled day."""
    client.put(
        "/api/v1/daily-production/schedule",
        json={"production_date": "2026-08-19", "drawers_scheduled": 100},
    )
    client.put(
        "/api/v1/daily-production/2026-08-20",
        json={
            "shift": "Day",
            "drawers_inspected": 90,
            "drawers_rejected_unique": 0,
            "drawers_reworked": 0,
        },
    )

    resp = client.get(
        "/api/v1/daily-production/schedule-attainment",
        params={"start_date": "2026-08-19", "end_date": "2026-08-20"},
    )
    days_by_date = {d["production_date"]: d for d in resp.json()["days"]}
    assert days_by_date["2026-08-19"]["drawers_scheduled"] == 100
    assert days_by_date["2026-08-19"]["drawers_inspected"] == 0
    assert days_by_date["2026-08-20"]["drawers_scheduled"] is None
    assert days_by_date["2026-08-20"]["drawers_inspected"] == 90


# ---------------------------------------------------------------------------
# GET /api/v1/reports/date-preset
# ---------------------------------------------------------------------------


def test_date_preset_today_returns_a_single_day_range(client):
    resp = client.get("/api/v1/reports/date-preset", params={"preset": "today"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["start_date"] == body["end_date"]


def test_date_preset_unknown_preset_returns_400(client):
    resp = client.get("/api/v1/reports/date-preset", params={"preset": "bogus"})
    assert resp.status_code == 400


def test_date_preset_yesterday_and_last_n_days_are_working_day_aware(client):
    """Smoke test only - can't pin an exact date without controlling "today",
    but every one of these must be a real weekday (Working Days Logic Part C
    addendum), never a Saturday/Sunday, whatever today happens to be. Full
    boundary coverage lives in tests/unit/test_working_days_service.py."""
    for preset in ("yesterday", "last_7_days", "last_30_days"):
        resp = client.get("/api/v1/reports/date-preset", params={"preset": preset})
        assert resp.status_code == 200
        body = resp.json()
        start = dt.date.fromisoformat(body["start_date"])
        end = dt.date.fromisoformat(body["end_date"])
        assert start <= end
        if preset == "yesterday":
            assert start == end
            assert start.weekday() < 5


def test_date_preset_this_week_is_monday_through_today(client):
    """Smoke test only - can't pin an exact date without controlling "today"
    (this endpoint always resolves against the real current instant). Full
    boundary coverage (Wednesday/Monday/Sunday) lives in
    tests/unit/test_date_presets.py."""
    resp = client.get("/api/v1/reports/date-preset", params={"preset": "this_week"})
    assert resp.status_code == 200
    body = resp.json()
    start = dt.date.fromisoformat(body["start_date"])
    end = dt.date.fromisoformat(body["end_date"])
    assert start.weekday() == 0  # Monday
    assert start <= end


def test_date_preset_last_week_is_always_a_five_day_mon_fri_span(client):
    resp = client.get("/api/v1/reports/date-preset", params={"preset": "last_week"})
    assert resp.status_code == 200
    body = resp.json()
    start = dt.date.fromisoformat(body["start_date"])
    end = dt.date.fromisoformat(body["end_date"])
    assert start.weekday() == 0  # Monday
    assert end.weekday() == 4  # Friday
    assert (end - start).days == 4
