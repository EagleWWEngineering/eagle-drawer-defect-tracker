"""Quality Today dashboard (2026-10 redesign): everything the page shows, in one call.

The dashboard is a glance-first page (desk and shop TV), so it takes no filters:
today, this week against the same days of last week, the last 20 working days,
and the things to chase (oldest open drawers, customer issues this week, feeds).
Every rate goes through metrics_service.compute_kpis (PROJECT_SPEC.md 2.1) with
the effective rejected count (defect_service.effective_rejected), the same as
Reports, so the two pages can never disagree.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter

from sqlalchemy.orm import Session

from app.models import (
    CustomerIssue,
    DailyProductionSummary,
    DailySchedule,
    DefectCase,
    DrawerEvent,
)
from app.services import (
    defect_service,
    feed_health_service,
    metrics_service,
    order_line_service,
    settings_service,
    working_days_service,
)
from app.services.defect_service import DIRECT_CLOSE_SOURCE_STATUSES
from app.timezone_utils import to_display_string

KICKBACK = "undo_card"


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _cases(db: Session, start: dt.date, end: dt.date) -> list[DefectCase]:
    return (
        db.query(DefectCase)
        .filter(
            DefectCase.is_deleted.is_(False),
            DefectCase.production_date >= start,
            DefectCase.production_date <= end,
        )
        .all()
    )


def _summaries(db: Session, start: dt.date, end: dt.date) -> list[DailyProductionSummary]:
    return (
        db.query(DailyProductionSummary)
        .filter(
            DailyProductionSummary.production_date >= start,
            DailyProductionSummary.production_date <= end,
        )
        .all()
    )


def _period(db: Session, start: dt.date, end: dt.date) -> dict:
    """Rates for [start, end] - the same formulas and sources as Reports."""
    cases = _cases(db, start, end)
    rows = _summaries(db, start, end)
    inspected = sum(r.drawers_inspected for r in rows)
    rejected = sum(defect_service.effective_rejected(db, rows).values())
    events = sum(i.affected_drawer_quantity for c in cases for i in c.items)
    kpis = metrics_service.compute_kpis(
        drawers_inspected=inspected,
        defect_events=events,
        unique_drawers_rejected=rejected,
        drawers_reworked=sum(1 for c in cases if c.disposition == "Rework"),
    ).to_dict()
    return {
        "start": start,
        "end": end,
        "drawers_inspected": inspected,
        "defect_events": events,
        "cases": len(cases),
        "unique_drawers_rejected": rejected,
        "kickbacks": sum(1 for c in cases if c.entry_source == KICKBACK),
        "defects_per_100": kpis["defects_per_100"],
        "rejection_rate": kpis["rejection_rate"],
        "first_pass_yield": kpis["first_pass_yield"],
    }


def _kickback_areas(db: Session, case_ids: list[int]) -> dict[int, str]:
    if not case_ids:
        return {}
    return dict(
        db.query(DrawerEvent.defect_case_id, DrawerEvent.area)
        .filter(DrawerEvent.defect_case_id.in_(case_ids), DrawerEvent.event_type == "kickback")
        .all()
    )


def _category_counts(cases: list[DefectCase]) -> Counter:
    counts: Counter = Counter()
    for c in cases:
        for i in c.items:
            counts[i.defect_category.name] += i.affected_drawer_quantity
    return counts


def build(db: Session, today: dt.date, *, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)

    # --- Today -----------------------------------------------------------------
    today_period = _period(db, today, today)
    schedule = db.get(DailySchedule, today)
    today_cases = _cases(db, today, today)
    areas = _kickback_areas(db, [c.id for c in today_cases if c.entry_source == KICKBACK])
    kick_assembly = sum(1 for v in areas.values() if v == "assembly")
    kick_today = sum(1 for c in today_cases if c.entry_source == KICKBACK)

    open_cases = (
        db.query(DefectCase)
        .filter(
            DefectCase.is_deleted.is_(False), DefectCase.status.in_(DIRECT_CLOSE_SOURCE_STATUSES)
        )
        .order_by(DefectCase.detected_at)
        .all()
    )
    oldest_open = open_cases[0] if open_cases else None

    # --- This week vs the same days last week ---------------------------------
    week_start = today - dt.timedelta(days=today.weekday())
    this_week = _period(db, week_start, today)
    last_week = _period(db, week_start - dt.timedelta(days=7), today - dt.timedelta(days=7))

    week_cases = _cases(db, week_start, today)
    last_week_cases = _cases(db, week_start - dt.timedelta(days=7), today - dt.timedelta(days=7))
    now_counts, before_counts = _category_counts(week_cases), _category_counts(last_week_cases)
    top = [
        {"label": name, "count": n, "delta": n - before_counts.get(name, 0)}
        for name, n in sorted(now_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    ]

    # --- Last 20 working days ---------------------------------------------------
    try:
        start20 = working_days_service.walk_back_working_days(db, today, 19)
    except Exception:  # noqa: BLE001 - sparse history: fall back to calendar days
        start20 = today - dt.timedelta(days=27)
    working = working_days_service.working_day_set(db, start20, today)
    span_cases = _cases(db, start20, today)
    span_rows = _summaries(db, start20, today)
    inspected_by_day: Counter = Counter()
    for r in span_rows:
        inspected_by_day[r.production_date] += r.drawers_inspected
    events_by_day: Counter = Counter()
    for c in span_cases:
        events_by_day[c.production_date] += sum(i.affected_drawer_quantity for i in c.items)
    trend = []
    for day in sorted(working | {today}):
        inspected = inspected_by_day.get(day, 0)
        trend.append(
            {
                "date": day,
                "defect_events": events_by_day.get(day, 0),
                "drawers_inspected": inspected,
                "defects_per_100": (events_by_day.get(day, 0) / inspected * 100)
                if inspected
                else None,
            }
        )

    # --- Kickbacks: last 10 working days by area, and by clamp this week ---------
    last10 = sorted(working | {today})[-10:]
    kick_span = [
        c for c in span_cases if c.entry_source == KICKBACK and c.production_date in last10
    ]
    kick_areas = _kickback_areas(db, [c.id for c in kick_span])
    kickbacks_by_day = []
    for day in last10:
        day_cases = [c for c in kick_span if c.production_date == day]
        assembly = sum(1 for c in day_cases if kick_areas.get(c.id) == "assembly")
        kickbacks_by_day.append(
            {"date": day, "assembly": assembly, "qc": len(day_cases) - assembly}
        )
    week_kicks = [c for c in week_cases if c.entry_source == KICKBACK]
    clamp_counts = Counter(order_line_service.clamp_by_case(db, week_kicks).values())
    kickbacks_by_clamp = [{"label": name, "count": n} for name, n in sorted(clamp_counts.items())]

    # --- Oldest open drawers ------------------------------------------------------
    oldest = open_cases[:5]
    info = order_line_service.drawer_info(db, oldest)
    oldest_areas = _kickback_areas(db, [c.id for c in oldest if c.entry_source == KICKBACK])
    oldest_out = [
        {
            "id": c.id,
            "case_number": c.case_number,
            "work_order_number": c.work_order_number,
            "line": info[c.id]["line"],
            "customer_name": info[c.id]["customer"],
            "categories": [i.defect_category.name for i in c.items],
            "priority": c.priority,
            "kind": "kickback" if c.entry_source == KICKBACK else "qc",
            "kickback_area": oldest_areas.get(c.id),
            "age_hours": round((now - _aware(c.detected_at)).total_seconds() / 3600, 1),
        }
        for c in oldest
    ]

    # --- Customer issues this week ---------------------------------------------------
    issues = (
        db.query(CustomerIssue)
        .filter(
            CustomerIssue.is_deleted.is_(False),
            CustomerIssue.status != "Ignored",
            CustomerIssue.reported_date >= week_start,
            CustomerIssue.reported_date <= today,
        )
        .order_by(CustomerIssue.reported_date.desc())
        .all()
    )
    issues_out = [
        {
            "customer_name": i.customer_name,
            "order_number": i.order_number,
            "category": i.issue_category.name,
            "pieces": i.piece_count,
            "should_have_caught_at": i.should_have_caught_at,
            "description": (i.description or "")[:140],
        }
        for i in issues
    ]

    return {
        "today": today,
        "generated_at_local": to_display_string(now),
        "target_per_100": settings_service.get_quality_target(db),
        "today_stats": {
            **today_period,
            "drawers_scheduled": schedule.drawers_scheduled if schedule else None,
            "kickbacks_assembly": kick_assembly,
            "kickbacks_qc": kick_today - kick_assembly,
            "open_cases": len(open_cases),
            "oldest_open_hours": round(
                (now - _aware(oldest_open.detected_at)).total_seconds() / 3600, 1
            )
            if oldest_open
            else None,
            "oldest_open_work_order": oldest_open.work_order_number if oldest_open else None,
        },
        "this_week": this_week,
        "last_week": last_week,
        "trend": trend,
        "top_defects": top,
        "kickbacks_by_day": kickbacks_by_day,
        "kickbacks_by_clamp": kickbacks_by_clamp,
        "oldest_open": oldest_out,
        "customer_issues": issues_out,
        "feed_health": feed_health_service.status(db, now=now),
    }
