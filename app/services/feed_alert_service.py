"""Slack alert for late or refused production count feeds (2026-10-09).

Every few minutes (app/main.py lifespan) the feed health from
feed_health_service is checked. A feed that turns late or refused gets ONE
message; when it is back to normal, one more. The feeds already alerted on are
kept in app_settings, so a restart or deploy never repeats a message. If the
Slack post fails, nothing is remembered and the next check tries again.

Posts with the VM's shared "Eagle Ops" bot (token read from
Settings.slack_token_file at send time). Off unless FEED_ALERT_ENABLED=true.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import AppSetting
from app.services import feed_health_service

logger = logging.getLogger(__name__)

ALERTED_KEY = "feed_alert_open"


def _alerted(db: Session) -> set[str]:
    setting = db.get(AppSetting, ALERTED_KEY)
    if setting is None or not setting.value:
        return set()
    return {f for f in setting.value.split(",") if f}


def _save_alerted(db: Session, feeds: set[str]) -> None:
    stored = ",".join(sorted(feeds))
    setting = db.get(AppSetting, ALERTED_KEY)
    if setting is None:
        db.add(AppSetting(key=ALERTED_KEY, value=stored))
    else:
        setting.value = stored
    db.commit()


def read_slack_token(settings: Settings) -> str | None:
    """SLACK_BOT_TOKEN from the shared gateway env file, or None."""
    try:
        text = Path(settings.slack_token_file).expanduser().read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, value = line.partition("=")
        if sep and key.strip() == "SLACK_BOT_TOKEN":
            return value.strip().strip('"').strip("'") or None
    return None


def _problem_line(feed: dict) -> str:
    last = feed["last_received_local"] or "never"
    if feed["state"] == "error":
        reason = feed["message"] or "no reason given"
        return f"• *{feed['label']}*: last push was refused ({reason}), {last}"
    return f"• *{feed['label']}*: nothing received since {last} ({feed['minutes_ago']} min ago)"


def alert_text(new_problems: list[dict], recovered: list[dict]) -> str:
    lines = []
    if new_problems:
        lines.append(":warning: *Defect tracker: production count feed problem*")
        lines += [_problem_line(f) for f in new_problems]
    if recovered:
        if lines:
            lines.append("")
        lines.append(":white_check_mark: *Defect tracker: back to normal*")
        lines += [f"• {f['label']}" for f in recovered]
    return "\n".join(lines)


def check_and_alert(
    db: Session,
    *,
    now: dt.datetime | None = None,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """One check. Returns a short outcome for the log; never raises on a Slack failure."""
    settings = settings or get_settings()
    feeds = feed_health_service.status(db, now=now)["feeds"]
    problems = {f["feed"]: f for f in feeds if f["state"] in ("late", "error")}
    alerted = _alerted(db)
    new_problems = [f for key, f in problems.items() if key not in alerted]
    recovered = [f for f in feeds if f["feed"] in alerted and f["feed"] not in problems]
    if not new_problems and not recovered:
        return "no change"
    token = read_slack_token(settings)
    if not token:
        return f"skipped: no SLACK_BOT_TOKEN in {settings.slack_token_file}"
    try:
        with httpx.Client(timeout=15, transport=transport) as client:
            resp = client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "channel": settings.feed_alert_channel,
                    "text": alert_text(new_problems, recovered),
                    "unfurl_links": False,
                },
            )
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return f"failed: {type(exc).__name__}: {exc}"
    if not body.get("ok"):
        return f"failed: Slack said {body.get('error')!r}"
    _save_alerted(db, set(problems))
    return f"posted: {len(new_problems)} problem(s), {len(recovered)} recovered"


async def run_periodic(session_factory, settings: Settings | None = None) -> None:
    """Background loop started by app.main's lifespan; cancelled on shutdown."""
    settings = settings or get_settings()
    interval = max(1, settings.feed_alert_check_minutes) * 60

    def _once() -> str:
        db = session_factory()
        try:
            return check_and_alert(db, settings=settings)
        finally:
            db.close()

    while True:
        await asyncio.sleep(interval)
        try:
            outcome = await asyncio.to_thread(_once)
        except Exception:
            logger.exception("Feed alert check crashed")
            continue
        if outcome != "no change":
            logger.info("Feed alert check: %s", outcome)
