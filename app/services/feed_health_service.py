"""Feed health (2026-10 redesign): is production count still talking to us?

Every push production count makes (app/routers/sync.py) records when it last
arrived and whether it was accepted, in feed_statuses - one row per feed. The
Dashboard and Admin show a health line from it; a feed counts as late when it
has not arrived for much longer than its schedule allows.

Kickback / +1-scan events are only sent when there are new scans, so a quiet
hour is normal for them: production count's 60-second heartbeat is the
"connection is alive" signal, and the events feed only ever shows a problem when
a push was refused.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from app.models import FeedStatus
from app.timezone_utils import to_display_string

# feed key -> (label for people, minutes after which it is late; None = never late)
FEEDS: dict[str, tuple[str, int | None]] = {
    "heartbeat": ("Connection to production count", 5),
    "drawer_events": ("Kickbacks and scans", None),
    "order_lines": ("Order lines and customers", 120),
    "daily_completed": ("Drawers completed", 18 * 60),
    "daily_schedule": ("Schedule", 120),
    "customer_issues": ("Customer issues", 120),
}


def record(db: Session, feed: str, *, ok: bool, message: str | None = None) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    row = db.get(FeedStatus, feed)
    if row is None:
        row = FeedStatus(feed=feed)
        db.add(row)
    row.last_received_at = now
    if ok:
        row.last_ok_at = now
    row.last_result = "ok" if ok else "error"
    row.last_message = (message or "")[:255] or None
    db.commit()


@contextmanager
def tracking(db: Session, feed: str) -> Iterator[None]:
    """Wrap one ingest: records ok when the body completes, error (with the reason)
    when it raises - then re-raises, so the caller's response is unchanged."""
    try:
        yield
    except Exception as exc:
        db.rollback()
        detail = getattr(exc, "message", None) or getattr(exc, "detail", None) or str(exc)
        record(db, feed, ok=False, message=f"refused: {detail}")
        raise
    else:
        record(db, feed, ok=True)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def status(db: Session, *, now: dt.datetime | None = None) -> dict:
    """{"ok": bool, "feeds": [{feed, label, state, last_received_local,
    minutes_ago, message}]}; state is ok | late | error | never."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = {r.feed: r for r in db.query(FeedStatus).all()}
    feeds = []
    for key, (label, late_after) in FEEDS.items():
        row = rows.get(key)
        received = _aware(row.last_received_at) if row else None
        minutes = round((now - received).total_seconds() / 60) if received else None
        if row is None:
            state = "never"
        elif row.last_result == "error":
            state = "error"
        elif late_after is not None and minutes is not None and minutes > late_after:
            state = "late"
        else:
            state = "ok"
        feeds.append(
            {
                "feed": key,
                "label": label,
                "state": state,
                "last_received_local": to_display_string(received) if received else None,
                "minutes_ago": minutes,
                "message": row.last_message if row else None,
            }
        )
    problems = [f for f in feeds if f["state"] in ("late", "error")]
    return {"ok": not problems, "feeds": feeds}
