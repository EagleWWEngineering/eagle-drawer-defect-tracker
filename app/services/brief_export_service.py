"""Brief Export (Part A): read-only summary served to the Eagle production
brief's drawers TV board - GET /api/v1/brief/summary (app/routers/brief.py).

Composes existing services rather than reimplementing their rules:
working_days_service for "what was the last working day" (never a local
`weekday() < 5` check), schedule_service for the advisory scheduled figure, and
metrics_service for the Pareto/counting math that already backs the Reports
page - so the number on the TV and the number on Reports can never disagree.

Writes nothing to the database. See docs/... (the Brief Export prompt) for the
full feature spec.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.errors import ServiceError, ValidationError
from app.models import DailyProductionSummary
from app.services import metrics_service, schedule_service, working_days_service

# The only supported `product` query values today. A module-level list (rather
# than an inline check) so adding "doors" later is a one-line addition here, not
# a rewrite - see the Brief Export prompt. Do not add "doors" or any
# doors-specific branching until that prompt actually ships.
SUPPORTED_PRODUCTS: tuple[str, ...] = ("drawers",)


def validate_product(product: str | None) -> None:
    """Raises ValidationError (-> 400, app's standard error envelope) for any
    value not in SUPPORTED_PRODUCTS - including None, which is how the router
    represents an omitted `product` query param (see app/routers/brief.py: it
    is deliberately NOT a plain required FastAPI parameter, specifically so a
    missing `product` fails this same business-rule check and gets this same
    400 envelope, rather than FastAPI's own 422 "missing query parameter"
    shape)."""
    if product not in SUPPORTED_PRODUCTS:
        raise ValidationError(
            f"Unsupported product {product!r} - expected one of {SUPPORTED_PRODUCTS}.",
            field="product",
        )


def _aggregate_case_and_event_counts(items: list) -> dict:
    """{cases, defect_events, category_counts} for an already-fetched list of
    (DefectItem, DefectCase) rows (see metrics_service.filtered_defect_items_
    query). The math itself - the ONE aggregation build_last_production_day
    (a single-day fetch), build_week_summary's week total, and its per-day
    `days` breakdown (_build_week_days) all call - so the per-day defect
    tile, the weekly Pareto, and the day-by-day chart can never drift apart
    from each other or from the Reports page.

    cases: count of distinct DefectCase ids among `items` (one case = one
    defective drawer). defect_events: sum of DefectItem.affected_drawer_
    quantity - one category on one drawer is one event; affected_drawer_
    quantity folds in multiple drawers of the same category on one case.
    category_counts is the {category name: event count} map build_week_
    summary feeds into metrics_service.compute_pareto for its top_categories/
    other_count - callers that only need cases/defect_events ignore it.

    An empty `items` list legitimately returns all-zero fields - a
    defect-free day/week is a real, verified result, never null.
    """
    case_ids = {case.id for _item, case in items}
    category_counts: dict[str, int] = {}
    for item, _case in items:
        label = item.defect_category.name
        category_counts[label] = category_counts.get(label, 0) + item.affected_drawer_quantity

    return {
        "cases": len(case_ids),
        "defect_events": sum(category_counts.values()),
        "category_counts": category_counts,
    }


def _case_and_event_counts(db: Session, start_date: dt.date, end_date: dt.date) -> dict:
    """_aggregate_case_and_event_counts over [start_date, end_date] inclusive,
    fetched via metrics_service.filtered_defect_items_query (the same
    non-deleted-case, date-ranged query Reports already uses). Used by
    build_last_production_day, which only needs a single date's figures and
    has no other query to share the fetch with (unlike build_week_summary,
    which fetches its own range once and reuses it for both the week total
    and the day-by-day breakdown - see _build_week_days)."""
    items = metrics_service.filtered_defect_items_query(
        db, start_date=start_date, end_date=end_date
    ).all()
    return _aggregate_case_and_event_counts(items)


def build_last_production_day(db: Session, asof: dt.date) -> dict | None:
    """{date, entered, inspected, scheduled_per_tracker, cases, defect_events}
    for the last WORKING day strictly before `asof`, resolved via
    working_days_service.previous_working_day - the single source of truth for
    "working day" (Mon->Fri on an ordinary week; a recorded Friday holiday walks
    back to Thursday; etc). This function never encodes its own definition of a
    working day.

    entered/inspected: entered is False and inspected is None (never 0) when no
    daily_production_summaries row exists for that date at all - "nobody
    entered it yet" must stay distinguishable from "we inspected zero drawers"
    all the way to the TV. When a row (or rows - one per shift) does exist,
    inspected sums drawers_inspected across every shift that date.

    scheduled_per_tracker is this app's own daily_schedules figure for that
    date (None if no row) - ADVISORY ONLY. This app's daily_schedules rows were
    originally scraped FROM the production brief, so sending them back for the
    brief to display would be circular; the brief displays its own sqlite
    figure for "scheduled" and is expected to use this one only to log a
    mismatch. Schedule attainment % is deliberately never computed in this app
    - see app/services/metrics_service.py compute_schedule_attainment_pct for
    where that math already lives, for this app's own Dashboard use only.

    cases/defect_events (added for the TV's "Yesterday's result" fourth tile):
    counts of distinct non-deleted DefectCase rows / defect events for this
    single date, via the SAME _case_and_event_counts helper build_week_summary
    uses - never a second aggregation that could drift from the weekly figures
    or the Reports page. These come from DefectCase, not
    daily_production_summaries, and are DELIBERATELY DECOUPLED from
    entered/inspected: entered=False never implies cases/defect_events are
    null, and inspected=None can coexist with a real, nonzero cases count (a
    day can have logged defect cases before the Daily Summary form was ever
    filled in). A day with genuinely no cases is cases=0/defect_events=0 - real
    zeros, same reasoning as build_week_summary's empty-range case, never null.

    Returns None (never lets a ServiceError escape) if previous_working_day
    can't find a working day within its lookback window - a brief that can't
    find "yesterday" should render "not available", not fail to generate. In
    that case cases/defect_events are simply absent too, folded into the whole
    object being None.
    """
    try:
        production_date = working_days_service.previous_working_day(db, asof)
    except ServiceError:
        return None

    rows = (
        db.query(DailyProductionSummary)
        .filter(DailyProductionSummary.production_date == production_date)
        .all()
    )
    entered = len(rows) > 0
    inspected = sum(r.drawers_inspected for r in rows) if entered else None

    schedule_row = schedule_service.get_schedule(db, production_date)
    scheduled_per_tracker = schedule_row.drawers_scheduled if schedule_row is not None else None

    day_counts = _case_and_event_counts(db, production_date, production_date)

    return {
        "date": production_date,
        "entered": entered,
        "inspected": inspected,
        "scheduled_per_tracker": scheduled_per_tracker,
        "cases": day_counts["cases"],
        "defect_events": day_counts["defect_events"],
    }


def _monday_of(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _week_range(asof: dt.date) -> tuple[dt.date, dt.date, str]:
    """(start, end, basis) for the "Internal QC - this week" section.

    Tue-Fri: "week_to_date", Monday of asof's week through asof inclusive.

    Monday: "prior_full_week", the Mon-Fri BEFORE asof's week - at 06:15
    Monday the current week has no data yet, so week_to_date would render an
    empty section every Monday.

    Sat/Sun, defensively (the brief only calls this on weekday mornings, but a
    stray asof shouldn't behave surprisingly): also "prior_full_week", but here
    that means the Mon-Fri of asof's OWN week (already fully elapsed by the
    weekend) - not the week before that, which would skip 5 days of real data
    for no reason.
    """
    this_monday = _monday_of(asof)
    if asof.weekday() == 0:  # Monday: the week before this one has no data yet
        start = this_monday - dt.timedelta(days=7)
    elif asof.weekday() >= 5:  # Sat/Sun: this week's Mon-Fri already happened
        start = this_monday
    else:
        return this_monday, asof, "week_to_date"
    return start, start + dt.timedelta(days=4), "prior_full_week"


def _build_week_days(db: Session, items: list, start: dt.date, end: dt.date) -> list[dict]:
    """{date, entered, inspected, cases} for every WORKING day in
    [start, end] - the day-by-day breakdown behind the brief's
    scheduled-vs-inspected bar chart (Part 2). One entry per working day, in
    ascending date order; `items` is the SAME (DefectItem, DefectCase) row
    list build_week_summary already fetched for the week total (never a
    second call to metrics_service.filtered_defect_items_query), so a day's
    `cases` and the week's `cases` structurally cannot drift apart: every
    DefectCase has exactly one production_date, so grouping `items` by date
    partitions the week's cases with no overlap and no gap -
    sum(day["cases"] for day in days) == week.cases always.

    Which dates count as working days comes from working_days_service.
    working_day_set - the single source of truth (never a local
    `weekday() < 5` check) - so a manually-entered overtime Saturday (or one
    with real inspections logged) appears here exactly when it would count
    as a working day anywhere else in this app, and an ordinary weekend date
    never does, even if it happens to carry defect cases (a case can be
    logged against any production_date regardless of whether that date is a
    working day).

    entered/inspected follow the exact same False/None-never-0 rule as
    build_last_production_day: entered is True only when at least one
    daily_production_summaries row exists for that date (drawers_inspected
    summed across shifts, via ONE grouped query over the whole range - never
    one query per day); no row at all is entered=False, inspected=None. This
    is deliberately independent of `cases` - a day can have logged defect
    cases before the Daily Summary form was ever filled in for it, same as
    build_last_production_day's single-day figures.

    `scheduled` is deliberately NOT included - see the module docstring and
    build_last_production_day's scheduled_per_tracker note: the brief
    supplies its own schedule figure (the one this app's daily_schedules
    rows were originally scraped from), so sending it back would be
    circular.

    Empty result (no working days in range at all, including start > end)
    returns [], never None - callers must not need a null check on top of an
    empty-list check.
    """
    working_days = sorted(working_days_service.working_day_set(db, start, end))
    if not working_days:
        return []

    inspected_by_date: dict[dt.date, int] = dict(
        db.query(
            DailyProductionSummary.production_date,
            func.sum(DailyProductionSummary.drawers_inspected),
        )
        .filter(
            DailyProductionSummary.production_date >= start,
            DailyProductionSummary.production_date <= end,
        )
        .group_by(DailyProductionSummary.production_date)
        .all()
    )

    grouped_items: dict[dt.date, list] = {}
    for item, case in items:
        grouped_items.setdefault(case.production_date, []).append((item, case))
    cases_by_date = {
        d: _aggregate_case_and_event_counts(pairs)["cases"] for d, pairs in grouped_items.items()
    }

    return [
        {
            "date": d,
            "entered": d in inspected_by_date,
            "inspected": int(inspected_by_date[d]) if d in inspected_by_date else None,
            "cases": cases_by_date.get(d, 0),
        }
        for d in working_days
    ]


def build_week_summary(db: Session, asof: dt.date) -> dict:
    """{start, end, basis, cases, defect_events, top_categories, other_count,
    days} for the "Internal QC - this week" section - see _week_range for
    the Mon-Fri window rules.

    Fetches metrics_service.filtered_defect_items_query for [start, end]
    exactly ONCE and reuses it for both the week-total aggregation
    (_aggregate_case_and_event_counts) and the per-day breakdown
    (_build_week_days) - never a second query for the same range, and never
    a second aggregation that could drift from either. Plus
    app/services/metrics_service.py's compute_pareto (the same
    descending-count/name-tiebreak sort Reports already uses) for
    top_categories - the number on the TV and the number on Reports must
    never disagree.

    cases: count of distinct DefectCase ids in range (one case = one defective
    drawer). defect_events: sum of DefectItem.affected_drawer_quantity in
    range - the same "defect events" figure app/routers/reports.py's
    get_summary/get_pareto already compute (one category on one drawer is one
    event; affected_drawer_quantity folds in multiple drawers of the same
    category on one case).

    top_categories: the top 3 rows from compute_pareto (already sorted count
    desc, then category name asc - a deterministic tiebreak so the board never
    flickers between two equal categories on consecutive days). other_count is
    every remaining event, so sum(top) + other_count == defect_events exactly.

    days: see _build_week_days - one entry per working day in [start, end],
    for the brief's scheduled-vs-inspected bar chart (Part 2). Deliberately
    excludes `scheduled` - the brief supplies its own.

    Zero cases in range legitimately returns all-zero/empty fields - a
    defect-free week is a real, verified result, unlike an un-entered
    inspection count, so 0 is correct here, never null.
    """
    start, end, basis = _week_range(asof)

    items = metrics_service.filtered_defect_items_query(db, start_date=start, end_date=end).all()
    counts = _aggregate_case_and_event_counts(items)
    defect_events = counts["defect_events"]
    pareto_rows = metrics_service.compute_pareto(counts["category_counts"])
    top_rows = pareto_rows[:3]
    top_categories = [{"name": r["label"], "count": r["defect_events"]} for r in top_rows]
    other_count = defect_events - sum(c["count"] for c in top_categories)

    return {
        "start": start,
        "end": end,
        "basis": basis,
        "cases": counts["cases"],
        "defect_events": defect_events,
        "top_categories": top_categories,
        "other_count": other_count,
        "days": _build_week_days(db, items, start, end),
    }


def build_brief_summary(
    db: Session, *, product: str, asof: dt.date, generated_at: dt.datetime
) -> dict:
    """Full payload for GET /api/v1/brief/summary. `product` must already be
    validated (validate_product) by the caller; `generated_at` is
    caller-supplied (normally dt.datetime.now(dt.timezone.utc)) so this stays
    deterministic/testable rather than reading the clock itself."""
    return {
        "ok": True,
        "product": product,
        "asof": asof,
        "generated_at": generated_at,
        "last_production_day": build_last_production_day(db, asof),
        "week": build_week_summary(db, asof),
    }
