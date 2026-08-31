"""Unit tests for scripts/relay_customer_issues.py's Working Days Logic (Part C
addendum) freshness guard: _parse_board_heading_month_day and the stale_today
behavior it feeds in _collect_schedule_entries.

Sample markup mirrors docs/PRODUCTION_BRIEF_SCHEDULE_SOURCE.md's real captured
response - HTML-entity-encoded apostrophe, an em dash, no year in the heading.
"""

from __future__ import annotations

import datetime as dt

import httpx

from scripts import relay_customer_issues as relay

BASE_URL = "http://production-brief.example"


def _board_html(*, weekday: str, month_abbr: str, day: int, count: int = 406) -> str:
    return (
        f"<section><h2>Today&#x27;s plan — {weekday} {month_abbr} {day}</h2>"
        '<div class="facts">'
        f'<div class="fact fact-none"><div class="fact-value">{count}</div>'
        '<div class="fact-label">drawers scheduled to finish today</div></div>'
        "</div></section>"
    )


# ---------------------------------------------------------------------------
# _parse_board_heading_month_day
# ---------------------------------------------------------------------------


def test_parses_the_real_sample_markup():
    html = _board_html(weekday="Friday", month_abbr="Aug", day=21)
    assert relay._parse_board_heading_month_day(html) == (8, 21)


def test_tolerant_of_a_different_separator_around_the_date():
    """The heading is a plain, untested f-string on the brief's side - a dash
    swap must not break parsing, since the parser only looks for the month+day
    pattern, not the exact separator."""
    html = '<section><h2>Today&#x27;s plan - Friday Aug 21</h2><div class="facts"></div></section>'
    assert relay._parse_board_heading_month_day(html) == (8, 21)


def test_returns_none_when_no_todays_plan_heading_exists():
    html = "<section><h2>Something else entirely</h2></section>"
    assert relay._parse_board_heading_month_day(html) is None


def test_returns_none_when_heading_has_no_recognizable_date():
    html = "<section><h2>Today&#x27;s plan — completely reformatted</h2></section>"
    assert relay._parse_board_heading_month_day(html) is None


def test_returns_none_on_empty_page():
    assert relay._parse_board_heading_month_day("") is None


def test_single_digit_day_parses():
    html = _board_html(weekday="Monday", month_abbr="Sep", day=1)
    assert relay._parse_board_heading_month_day(html) == (9, 1)


# ---------------------------------------------------------------------------
# _collect_schedule_entries: stale_today behavior (LIVE fetch only)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def _patch_httpx_get(monkeypatch, *, live_html: str, live_status: int = 200):
    """Archive requests (back > 0) always 404 - not_found, harmless, and
    irrelevant to the freshness guard (which only ever looks at day == today).
    Only the live /drawers.html request returns the caller-supplied body."""

    def fake_get(url, timeout=None):
        if url == f"{BASE_URL}/drawers.html":
            return _FakeResponse(live_status, live_html)
        return _FakeResponse(404, "")

    monkeypatch.setattr(httpx, "get", fake_get)


def test_matching_heading_is_not_flagged_stale(monkeypatch):
    today = dt.date.today()
    html = _board_html(
        weekday=today.strftime("%A"), month_abbr=today.strftime("%b"), day=today.day, count=406
    )
    _patch_httpx_get(monkeypatch, live_html=html)

    entries, found, unreachable, stale_today = relay._collect_schedule_entries(BASE_URL)

    assert stale_today is False
    today_entry = next(e for e in entries if e["date"] == today.isoformat())
    assert today_entry["drawers_scheduled"] == 406
    assert found == 1  # only today's live fetch had a real fact; archive 404s don't count


def test_mismatched_heading_is_flagged_stale_and_nulls_the_write(monkeypatch):
    today = dt.date.today()
    stale_day = today - dt.timedelta(days=1)  # always a different (month, day)
    html = _board_html(
        weekday=stale_day.strftime("%A"),
        month_abbr=stale_day.strftime("%b"),
        day=stale_day.day,
        count=406,
    )
    _patch_httpx_get(monkeypatch, live_html=html)

    entries, found, unreachable, stale_today = relay._collect_schedule_entries(BASE_URL)

    assert stale_today is True
    today_entry = next(e for e in entries if e["date"] == today.isoformat())
    assert today_entry["drawers_scheduled"] is None  # never written
    assert found == 0


def test_unparseable_heading_falls_back_to_writing_the_value(monkeypatch):
    """A parse failure must never block the write - see
    _parse_board_heading_month_day's docstring."""
    html = (
        '<section><h2>Something else entirely</h2><div class="facts">'
        '<div class="fact fact-none"><div class="fact-value">406</div>'
        '<div class="fact-label">drawers scheduled to finish today</div></div>'
        "</div></section>"
    )
    _patch_httpx_get(monkeypatch, live_html=html)

    today = dt.date.today()
    entries, found, unreachable, stale_today = relay._collect_schedule_entries(BASE_URL)

    assert stale_today is False
    today_entry = next(e for e in entries if e["date"] == today.isoformat())
    assert today_entry["drawers_scheduled"] == 406
