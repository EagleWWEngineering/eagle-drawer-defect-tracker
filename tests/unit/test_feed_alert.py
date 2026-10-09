"""Slack alert for late / refused production count feeds (feed_alert_service)."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from app.config import Settings
from app.models import FeedStatus
from app.services import feed_alert_service

NOW = dt.datetime(2026, 10, 9, 15, 0, tzinfo=dt.timezone.utc)


@pytest.fixture()
def settings(tmp_path):
    token_file = tmp_path / "gateway.env"
    token_file.write_text("export SLACK_BOT_TOKEN='xoxb-test'\n", encoding="utf-8")
    s = Settings()
    s.slack_token_file = str(token_file)
    s.feed_alert_channel = "U0BFBDY3QBF"
    return s


class FakeSlack:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.posts: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.posts.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"ok": self.ok} if self.ok else {"ok": False, "error": "channel_not_found"},
            )

        return httpx.MockTransport(handler)


def _feed(db, feed: str, minutes_ago: int, result: str = "ok", message: str | None = None):
    at = NOW - dt.timedelta(minutes=minutes_ago)
    db.add(
        FeedStatus(
            feed=feed, last_received_at=at, last_ok_at=at, last_result=result, last_message=message
        )
    )
    db.commit()


def _all_feeds_fresh(db):
    for feed in (
        "heartbeat",
        "drawer_events",
        "order_lines",
        "daily_completed",
        "daily_schedule",
        "customer_issues",
    ):
        _feed(db, feed, 1)


def test_all_fresh_posts_nothing(db_session, settings):
    _all_feeds_fresh(db_session)
    slack = FakeSlack()
    assert (
        feed_alert_service.check_and_alert(
            db_session, now=NOW, settings=settings, transport=slack.transport()
        )
        == "no change"
    )
    assert slack.posts == []


def test_late_feed_posts_once_then_recovery_once(db_session, settings):
    _all_feeds_fresh(db_session)
    db_session.get(FeedStatus, "heartbeat").last_received_at = NOW - dt.timedelta(minutes=20)
    db_session.commit()
    slack = FakeSlack()

    out = feed_alert_service.check_and_alert(
        db_session, now=NOW, settings=settings, transport=slack.transport()
    )
    assert out.startswith("posted: 1 problem")
    assert slack.posts[0]["channel"] == "U0BFBDY3QBF"
    assert "Connection to production count" in slack.posts[0]["text"]
    assert "20 min ago" in slack.posts[0]["text"]

    # Still late on the next check: no second message.
    feed_alert_service.check_and_alert(
        db_session, now=NOW, settings=settings, transport=slack.transport()
    )
    assert len(slack.posts) == 1

    db_session.get(FeedStatus, "heartbeat").last_received_at = NOW
    db_session.commit()
    out = feed_alert_service.check_and_alert(
        db_session, now=NOW, settings=settings, transport=slack.transport()
    )
    assert out == "posted: 0 problem(s), 1 recovered"
    assert "back to normal" in slack.posts[1]["text"]

    feed_alert_service.check_and_alert(
        db_session, now=NOW, settings=settings, transport=slack.transport()
    )
    assert len(slack.posts) == 2


def test_refused_push_is_reported_with_its_reason(db_session, settings):
    _all_feeds_fresh(db_session)
    row = db_session.get(FeedStatus, "drawer_events")
    row.last_result = "error"
    row.last_message = "refused: bad payload"
    db_session.commit()
    slack = FakeSlack()
    feed_alert_service.check_and_alert(
        db_session, now=NOW, settings=settings, transport=slack.transport()
    )
    assert "Kickbacks and scans" in slack.posts[0]["text"]
    assert "refused: bad payload" in slack.posts[0]["text"]


def test_failed_post_is_retried_next_check(db_session, settings):
    _feed(db_session, "heartbeat", 30)
    failing = FakeSlack(ok=False)
    out = feed_alert_service.check_and_alert(
        db_session, now=NOW, settings=settings, transport=failing.transport()
    )
    assert out == "failed: Slack said 'channel_not_found'"
    working = FakeSlack()
    feed_alert_service.check_and_alert(
        db_session, now=NOW, settings=settings, transport=working.transport()
    )
    assert len(working.posts) == 1


def test_no_token_file_skips_without_remembering(db_session, settings, tmp_path):
    _feed(db_session, "heartbeat", 30)
    settings.slack_token_file = str(tmp_path / "missing.env")
    out = feed_alert_service.check_and_alert(db_session, now=NOW, settings=settings)
    assert out.startswith("skipped: no SLACK_BOT_TOKEN")
    assert feed_alert_service._alerted(db_session) == set()


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("FEED_ALERT_ENABLED", raising=False)
    assert Settings().feed_alert_enabled is False


async def test_lifespan_starts_and_stops_the_alert_loop(monkeypatch):
    import asyncio

    import app.main as main_module
    from tests.unit.test_sync_lifespan import _make_test_sessionmaker

    monkeypatch.setattr(main_module, "SessionLocal", _make_test_sessionmaker())
    monkeypatch.setattr(main_module.settings, "feed_alert_enabled", True)
    started = asyncio.Event()

    async def fake_run_periodic(session_factory, settings=None):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(feed_alert_service, "run_periodic", fake_run_periodic)
    async with main_module.lifespan(main_module.app):
        await asyncio.wait_for(started.wait(), 1)
