"""Unit tests for app/services/sync_service.py (Phase 3).

All HTTP calls to the production brief are mocked via httpx.MockTransport - no
real network access is required or attempted.
"""

from __future__ import annotations

import datetime as dt
import decimal

import httpx
import pytest

from app.models import CustomerIssue, SyncLog
from app.services import sync_service


def _client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://fake-brief")


def _issue_payload(**overrides) -> dict:
    payload = {
        "thread_id": "thread-1",
        "day": "2026-07-25",
        "customer": "Armadio IQC",
        "order_no": None,
        "summary": "Drawer fronts off-dimension.",
        "category": "manufacturing",
        "subcategory": "wrong size",
        "station": "QA/Final",
        "rework_cost": 300,
        "cost_note": "3 pc x $100 base rate",
        "photos": 3,
        "photos_json": '["url1", "url2", "url3"]',
        "hubspot_url": "https://example.invalid/thread-1",
        "confidence": 0.92,
        "needs_review": False,
        "ignored": False,
        "first_seen": "2026-07-25T11:02:00Z",
        "received_at": "2026-07-25T11:00:00Z",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# fetch_issues
# ---------------------------------------------------------------------------


async def test_fetch_issues_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["since"] == "2026-07-01"
        assert request.url.params["include_ignored"] == "false"
        assert request.url.params["limit"] == "500"
        return httpx.Response(200, json={"ok": True, "count": 1, "issues": [_issue_payload()]})

    async with _client_for(handler) as client:
        data = await sync_service.fetch_issues(dt.date(2026, 7, 1), client=client)
    assert data["count"] == 1
    assert len(data["issues"]) == 1


async def test_fetch_issues_non_200_raises_clear_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    async with _client_for(handler) as client:
        with pytest.raises(sync_service.ProductionBriefError, match="HTTP 404"):
            await sync_service.fetch_issues(dt.date(2026, 7, 1), client=client)


async def test_fetch_issues_malformed_json_raises_clear_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json{{{")

    async with _client_for(handler) as client:
        with pytest.raises(sync_service.ProductionBriefError, match="malformed JSON"):
            await sync_service.fetch_issues(dt.date(2026, 7, 1), client=client)


async def test_fetch_issues_missing_issues_key_raises_clear_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with _client_for(handler) as client:
        with pytest.raises(sync_service.ProductionBriefError, match="issues"):
            await sync_service.fetch_issues(dt.date(2026, 7, 1), client=client)


async def test_fetch_issues_connection_error_raises_clear_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _client_for(handler) as client:
        with pytest.raises(sync_service.ProductionBriefError, match="Could not reach"):
            await sync_service.fetch_issues(dt.date(2026, 7, 1), client=client)


async def test_fetch_issues_timeout_raises_clear_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    async with _client_for(handler) as client:
        with pytest.raises(sync_service.ProductionBriefError, match="Timed out"):
            await sync_service.fetch_issues(dt.date(2026, 7, 1), client=client)


# ---------------------------------------------------------------------------
# piece_count parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cost_note,expected",
    [
        ("3 pc x $100 base rate", 3),
        ("3 pc × $100 base rate...", 3),
        ("12pc flat", 12),
        (None, 1),
        ("", 1),
        ("no digits here", 1),
        ("$100 flat fee", 1),
    ],
)
def test_parse_piece_count(cost_note, expected):
    assert sync_service._parse_piece_count(cost_note) == expected


# ---------------------------------------------------------------------------
# category / source_type mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subcategory,expected_category",
    [
        ("wrong size", "Wrong Size"),
        ("Wrong Size", "Wrong Size"),
        ("wrong spec", "Wrong Spec"),
        ("joinery", "Joinery"),
        ("out of square", "Joinery"),
        ("finish quality", "Finish Quality"),
        ("finish", "Finish Quality"),
        ("missing parts", "Missing Parts"),
        ("crushed box", "Shipping Damage / Crushed Box"),
        ("shipping damage", "Shipping Damage / Crushed Box"),
        ("corner impact", "Corner Impact"),
        ("warp or crack", "Warp or Crack"),
        ("warp", "Warp or Crack"),
        ("crack", "Warp or Crack"),
        ("hinge", "Hinge Holes"),
        ("hinge holes", "Hinge Holes"),
        ("something totally unrecognized", "Other"),
        (None, "Other"),
        # Real production brief data uses hyphens, not spaces (found via a real
        # sync against real seeded data - see docs/PROJECT_SPEC_PHASE3.md).
        ("wrong-size", "Wrong Size"),
        ("finish-quality", "Finish Quality"),
        ("wrong-spec", "Wrong Spec"),
        ("missing-parts", "Missing Parts"),
        ("crushed-box", "Shipping Damage / Crushed Box"),
        ("corner-impact", "Corner Impact"),
        ("other-transit", "Other"),
        ("out_of_square", "Joinery"),
    ],
)
def test_map_issue_fields_category_mapping(
    db_session, customer_categories, subcategory, expected_category
):
    fields = sync_service.map_issue_fields(db_session, _issue_payload(subcategory=subcategory))
    assert fields["issue_category_id"] == customer_categories[expected_category].id


@pytest.mark.parametrize(
    "brief_category,expected_source_type",
    [
        ("manufacturing", "Manufacturing"),
        ("shipping-damage", "Shipping Damage"),
        ("something-else", "Manufacturing"),
        (None, "Manufacturing"),
    ],
)
def test_map_issue_fields_source_type_mapping(
    db_session, customer_categories, brief_category, expected_source_type
):
    fields = sync_service.map_issue_fields(db_session, _issue_payload(category=brief_category))
    assert fields["source_type"] == expected_source_type


def test_map_issue_fields_rework_cost_provided_vs_auto_calculated(db_session, customer_categories):
    with_cost = sync_service.map_issue_fields(db_session, _issue_payload(rework_cost=250))
    assert with_cost["estimated_rework_cost"] == decimal.Decimal("250")

    without_cost = sync_service.map_issue_fields(
        db_session, _issue_payload(thread_id="thread-2", rework_cost=None, cost_note="4 pc")
    )
    assert without_cost["estimated_rework_cost"] == decimal.Decimal("400.00")


def test_map_issue_fields_ignored_and_needs_review(db_session, customer_categories):
    ignored = sync_service.map_issue_fields(db_session, _issue_payload(ignored=True))
    assert ignored["status"] == "Ignored"

    open_issue = sync_service.map_issue_fields(db_session, _issue_payload(ignored=False))
    assert open_issue["status"] == "Open"

    reviewed = sync_service.map_issue_fields(db_session, _issue_payload(needs_review=True))
    assert reviewed["needs_review_note"] == sync_service.NEEDS_REVIEW_NOTE

    not_reviewed = sync_service.map_issue_fields(db_session, _issue_payload(needs_review=False))
    assert not_reviewed["needs_review_note"] is None


def test_map_issue_fields_missing_thread_id_raises():
    with pytest.raises(sync_service.ProductionBriefError):
        sync_service.map_issue_fields(None, _issue_payload(thread_id=None))


def test_map_issue_fields_missing_day_raises(db_session, customer_categories):
    with pytest.raises(sync_service.ProductionBriefError):
        sync_service.map_issue_fields(db_session, _issue_payload(day=None))


def test_map_issue_fields_null_order_number_preserved(db_session, customer_categories):
    fields = sync_service.map_issue_fields(db_session, _issue_payload(order_no=None))
    assert fields["order_number"] is None


# ---------------------------------------------------------------------------
# validate_raw_payload
# ---------------------------------------------------------------------------


def test_validate_raw_payload_accepts_well_formed_dict():
    data = {"ok": True, "issues": []}
    assert sync_service.validate_raw_payload(data) is data


@pytest.mark.parametrize(
    "bad_payload",
    [
        "not-a-dict",
        [],
        {},
        {"count": 0},
        {"issues": "not-a-list"},
        {"issues": None},
        {"issues": {"nope": True}},
    ],
)
def test_validate_raw_payload_rejects_malformed(bad_payload):
    with pytest.raises(sync_service.ProductionBriefError):
        sync_service.validate_raw_payload(bad_payload)


# ---------------------------------------------------------------------------
# process_issues_payload: the shared mapping/dedup/upsert logic, used directly
# (independent of HTTP fetching) by the relay ingest endpoint.
# ---------------------------------------------------------------------------


def test_process_issues_payload_creates_new_issue_and_logs_success(db_session):
    payload = {"ok": True, "count": 1, "issues": [_issue_payload()]}

    log = sync_service.process_issues_payload(
        db_session, payload, source_url="relay:http://fake-brief/api/quality-issues"
    )

    assert log.status == "success"
    assert log.source_url == "relay:http://fake-brief/api/quality-issues"
    assert log.records_fetched == 1
    assert log.records_created == 1
    assert log.records_updated == 0
    assert log.records_skipped == 0

    issue = (
        db_session.query(CustomerIssue).filter(CustomerIssue.source_thread_id == "thread-1").first()
    )
    assert issue is not None
    assert issue.issue_number.startswith("CI-20260725-")
    assert issue.customer_name == "Armadio IQC"


def test_process_issues_payload_twice_does_not_duplicate(db_session):
    payload = {"issues": [_issue_payload()]}

    log1 = sync_service.process_issues_payload(db_session, payload, source_url="relay:x")
    log2 = sync_service.process_issues_payload(db_session, payload, source_url="relay:x")

    assert log1.records_created == 1
    assert log2.records_created == 0
    assert log2.records_updated == 1

    count = (
        db_session.query(CustomerIssue).filter(CustomerIssue.source_thread_id == "thread-1").count()
    )
    assert count == 1


def test_process_issues_payload_skips_bad_record_but_processes_others(db_session):
    bad = _issue_payload(thread_id=None)
    good = _issue_payload(thread_id="thread-good")
    payload = {"issues": [bad, good]}

    log = sync_service.process_issues_payload(db_session, payload, source_url="relay:x")

    assert log.records_skipped == 1
    assert log.records_created == 1
    assert log.errors is not None
    assert (
        db_session.query(CustomerIssue)
        .filter(CustomerIssue.source_thread_id == "thread-good")
        .count()
        == 1
    )


def test_process_issues_payload_empty_issues_list_is_a_success(db_session):
    log = sync_service.process_issues_payload(db_session, {"issues": []}, source_url="relay:x")
    assert log.status == "success"
    assert log.records_fetched == 0
    assert log.records_created == 0


# ---------------------------------------------------------------------------
# run_sync: dedup, create-vs-update, preserving local edits, error handling
# ---------------------------------------------------------------------------


def _mock_sync(monkeypatch, handler):
    def fake_build_client() -> httpx.AsyncClient:
        return _client_for(handler)

    monkeypatch.setattr(sync_service, "_build_client", fake_build_client)


async def test_run_sync_creates_new_issue_and_logs_success(db_session, monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "count": 1, "issues": [_issue_payload()]})

    _mock_sync(monkeypatch, handler)

    log = await sync_service.run_sync(db_session, since=dt.date(2026, 7, 1))
    assert log.status == "success"
    assert log.records_created == 1
    assert log.records_updated == 0

    issue = (
        db_session.query(CustomerIssue).filter(CustomerIssue.source_thread_id == "thread-1").first()
    )
    assert issue is not None
    assert issue.issue_number.startswith("CI-20260725-")
    assert issue.customer_name == "Armadio IQC"


async def test_run_sync_twice_does_not_duplicate(db_session, monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "count": 1, "issues": [_issue_payload()]})

    _mock_sync(monkeypatch, handler)

    log1 = await sync_service.run_sync(db_session, since=dt.date(2026, 7, 1))
    log2 = await sync_service.run_sync(db_session, since=dt.date(2026, 7, 1))

    assert log1.records_created == 1
    assert log2.records_created == 0
    assert log2.records_updated == 1

    count = (
        db_session.query(CustomerIssue).filter(CustomerIssue.source_thread_id == "thread-1").count()
    )
    assert count == 1


async def test_run_sync_preserves_linked_status_and_staff_notes(db_session, monkeypatch):
    def first_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "count": 1, "issues": [_issue_payload()]})

    _mock_sync(monkeypatch, first_handler)
    await sync_service.run_sync(db_session, since=dt.date(2026, 7, 1))

    issue = (
        db_session.query(CustomerIssue).filter(CustomerIssue.source_thread_id == "thread-1").first()
    )
    issue.status = "Linked"
    issue.linked_defect_case_id = None  # no real case needed for this test
    issue.notes = "Confirmed with QC, matched to case DF-123 by hand."
    db_session.commit()

    def second_handler(_request: httpx.Request) -> httpx.Response:
        updated = _issue_payload(customer="Armadio IQC (renamed)", ignored=True, needs_review=True)
        return httpx.Response(200, json={"ok": True, "count": 1, "issues": [updated]})

    _mock_sync(monkeypatch, second_handler)
    await sync_service.run_sync(db_session, since=dt.date(2026, 7, 1))

    db_session.refresh(issue)
    assert issue.customer_name == "Armadio IQC (renamed)"  # brief-sourced field updates
    assert issue.status == "Linked"  # staff status change survives, even though brief says ignored
    assert "Confirmed with QC" in issue.notes  # staff note preserved
    assert sync_service.NEEDS_REVIEW_NOTE in issue.notes  # new flag prepended, not overwriting


async def test_run_sync_open_issue_can_be_auto_ignored(db_session, monkeypatch):
    """An issue still in the untouched "Open" state CAN be moved to Ignored by a
    later sync - only staff-driven progress (Linked) is protected."""

    def first_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True, "count": 1, "issues": [_issue_payload(ignored=False)]}
        )

    _mock_sync(monkeypatch, first_handler)
    await sync_service.run_sync(db_session, since=dt.date(2026, 7, 1))

    def second_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True, "count": 1, "issues": [_issue_payload(ignored=True)]}
        )

    _mock_sync(monkeypatch, second_handler)
    await sync_service.run_sync(db_session, since=dt.date(2026, 7, 1))

    issue = (
        db_session.query(CustomerIssue).filter(CustomerIssue.source_thread_id == "thread-1").first()
    )
    assert issue.status == "Ignored"


async def test_run_sync_skips_bad_record_but_processes_others(db_session, monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        bad = _issue_payload(thread_id=None)
        good = _issue_payload(thread_id="thread-good")
        return httpx.Response(200, json={"ok": True, "count": 2, "issues": [bad, good]})

    _mock_sync(monkeypatch, handler)
    log = await sync_service.run_sync(db_session, since=dt.date(2026, 7, 1))

    assert log.records_skipped == 1
    assert log.records_created == 1
    assert log.errors is not None
    assert (
        db_session.query(CustomerIssue)
        .filter(CustomerIssue.source_thread_id == "thread-good")
        .count()
        == 1
    )


async def test_run_sync_unreachable_brief_logs_failure_without_raising(db_session, monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _mock_sync(monkeypatch, handler)
    log = await sync_service.run_sync(db_session, since=dt.date(2026, 7, 1))

    assert log.status == "failed"
    assert "Could not reach" in log.errors
    assert log.sync_completed_at is not None

    stored = db_session.query(SyncLog).filter(SyncLog.id == log.id).first()
    assert stored is not None
    assert stored.status == "failed"


async def test_run_sync_default_since_bootstraps_when_no_prior_success(db_session, monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["since"] = request.url.params["since"]
        return httpx.Response(200, json={"ok": True, "count": 0, "issues": []})

    _mock_sync(monkeypatch, handler)
    await sync_service.run_sync(db_session)  # no explicit `since`

    expected = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    assert captured["since"] == expected


async def test_run_sync_uses_last_successful_sync_date_when_available(db_session, monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "count": 0, "issues": []})

    _mock_sync(monkeypatch, handler)
    first_log = await sync_service.run_sync(db_session, since=dt.date(2026, 7, 1))
    assert first_log.status == "success"

    captured = {}

    def handler2(request: httpx.Request) -> httpx.Response:
        captured["since"] = request.url.params["since"]
        return httpx.Response(200, json={"ok": True, "count": 0, "issues": []})

    _mock_sync(monkeypatch, handler2)
    await sync_service.run_sync(db_session)  # no explicit `since` this time

    expected = first_log.sync_completed_at.date() - dt.timedelta(
        days=sync_service.SINCE_LOOKBACK_BUFFER_DAYS
    )
    assert captured["since"] == expected.isoformat()


async def test_run_sync_default_since_looks_back_past_last_success_date(db_session, monkeypatch):
    """A brief that classifies an issue a day or two after it was received (see
    SINCE_LOOKBACK_BUFFER_DAYS) must not be permanently skipped just because an
    earlier, empty-result sync's completion date already moved the cursor past
    the day that issue is tagged with."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "count": 0, "issues": []})

    _mock_sync(monkeypatch, handler)
    first_log = await sync_service.run_sync(db_session, since=dt.date(2026, 7, 28))
    assert first_log.status == "success"

    captured = {}

    def handler2(request: httpx.Request) -> httpx.Response:
        captured["since"] = request.url.params["since"]
        return httpx.Response(200, json={"ok": True, "count": 0, "issues": []})

    _mock_sync(monkeypatch, handler2)
    await sync_service.run_sync(db_session)  # no explicit `since`

    # sync_completed_at is stamped with the real clock at run time, not with the
    # `since` passed above - it has no bearing on the next default `since`. What
    # must hold is the buffer invariant itself: the next default `since` sits
    # strictly before (i.e. "looks back past") the prior sync's completion date.
    since_used = dt.date.fromisoformat(captured["since"])
    expected = first_log.sync_completed_at.date() - dt.timedelta(
        days=sync_service.SINCE_LOOKBACK_BUFFER_DAYS
    )
    assert since_used == expected
    assert since_used < first_log.sync_completed_at.date()
