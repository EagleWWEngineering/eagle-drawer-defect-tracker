"""Core business rules: case numbering, defect-case creation, and status transitions.

This is the ONE place the counting rules (PROJECT_SPEC.md section 2) and the status
transition map (PROJECT_SPEC.md section 3.1) are implemented. Routers call these
functions instead of touching the rules themselves, and the MCP server calls the
REST API (which calls these same functions) so the UI and MCP path can never drift
apart.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.errors import InvalidTransitionError, NotFoundError, ValidationError
from app.models import (
    DailyProductionSummary,
    DefectCase,
    DefectCategory,
    DefectItem,
    Station,
    StatusHistory,
)
from app.services import settings_service

VALID_PRIORITIES: list[str] = ["Urgent", "High", "Normal"]

# PROJECT_SPEC_PHASE7.md: statuses/dispositions simplified to a small vocabulary
# for new entry. Retired values remain valid STORED data (never dropped/rewritten
# on existing rows - see the Phase 7 migration) and must still render/filter/export
# correctly, but nothing new is ever written with them - see ALL_KNOWN_STATUSES /
# ALL_KNOWN_DISPOSITIONS below, used only for filter/display surfaces, never for
# write validation.
VALID_STATUSES: list[str] = ["Open", "Closed - Repaired", "Closed - Use As Is"]
RETIRED_STATUSES: list[str] = ["In Rework", "Waiting", "Ready for QC Recheck", "Closed - Scrapped"]
ALL_KNOWN_STATUSES: list[str] = VALID_STATUSES + RETIRED_STATUSES

# Rework is the default/primary disposition (PROJECT_SPEC.md section 3.2 /
# PROJECT_SPEC_PHASE7.md): on the shop floor, an operator who finds a problem
# almost always either re-cuts a new component or reworks the part in hand -
# that's the whole point of the two-option model. Set Aside is everything else
# (the old Use As Is / Hold / Scrap all collapse into "not being worked right
# now"). List order drives the New Defect form's button order/prominence.
VALID_DISPOSITIONS: list[str] = ["Rework", "Set Aside"]
RETIRED_DISPOSITIONS: list[str] = ["Use As Is", "Hold", "Scrap"]
ALL_KNOWN_DISPOSITIONS: list[str] = VALID_DISPOSITIONS + RETIRED_DISPOSITIONS

# Every historically-possible closed status, including the retired "Closed -
# Scrapped" - a case already sitting there (untouched by the Phase 7 migration,
# which only ever moves NON-closed cases) must still behave as closed everywhere
# this set is consulted (is_reopen, cost calc, etc.).
CLOSED_STATUSES: set[str] = {"Closed - Repaired", "Closed - Scrapped", "Closed - Use As Is"}

# The two closed statuses any NEW closure can target going forward. Deliberately
# excludes the retired "Closed - Scrapped" - no code path writes a case into it
# anymore (Scrap was retired as a disposition), though one already sitting there
# from before this change is untouched and can still be reopened (is_reopen below
# doesn't care which closed status a case is reopened FROM).
NEW_CLOSE_STATUSES: set[str] = {"Closed - Repaired", "Closed - Use As Is"}

# PROJECT_SPEC_PHASE7.md: the new status map has exactly two kinds of transition -
# direct-close (see direct_close_statuses below) and reopen (is_reopen in
# update_case_status). There is no more generic "move to an intermediate open
# status" transition at all (no more Waiting/In Rework/Ready for QC Recheck to
# move into), so every entry here is empty. Kept as an explicit table (rather than
# just falling through allowed_next_statuses' .get default) purely for
# documentation/parity with the historical map in PROJECT_SPEC.md section 3.1.
STATUS_TRANSITIONS: dict[str, set[str]] = {
    "Open": set(),
    "In Rework": set(),
    "Waiting": set(),
    "Ready for QC Recheck": set(),
    "Closed - Repaired": set(),
    "Closed - Scrapped": set(),
    "Closed - Use As Is": set(),
}

# PROJECT_SPEC_PHASE7.md "Cost model"/entry flow: when disposition="Rework" and
# resolved_on_the_spot=True, the New Defect form chooses which terminal status the
# case closes into - "Repaired" (default, pre-selected) or "Use As Is" (the
# DECISION-FLAGGED entry point for recording "defect exists, we're shipping it as
# is" without a separate disposition - see docs/PROJECT_SPEC_PHASE7.md). Any other
# disposition (Set Aside) has no instant-close path at all - it always lands Open.
INSTANT_CLOSE_OUTCOMES: dict[str, str] = {
    "Repaired": "Closed - Repaired",
    "Use As Is": "Closed - Use As Is",
}
DEFAULT_INSTANT_CLOSE_OUTCOME = "Repaired"

# StatusHistory note written for the instant-close fast path, instead of the generic
# "Case created" note every other new case gets - this is how the audit trail (and
# the "% Resolved On The Spot" KPI's underlying DefectCase.resolved_on_the_spot flag)
# distinguish a case that skipped the queue entirely from one that went through it.
RESOLVED_ON_THE_SPOT_NOTE = "Resolved on the spot at entry"

# PROJECT_SPEC_PHASE7.md: every non-closed status can close DIRECTLY to either of
# the two new closed statuses - the standard (only) way to close a case now. Still
# includes the retired open-ish statuses (In Rework/Waiting/Ready for QC Recheck)
# defensively: the Phase 7 migration moves every currently-open legacy-status case
# to Open, so none of these should exist going forward, but a case that somehow
# still carries one of them must still be closeable rather than stuck. The note is
# optional here (unlike reopening a closed case, which always requires one).
DIRECT_CLOSE_SOURCE_STATUSES: set[str] = {"Open", "In Rework", "Waiting", "Ready for QC Recheck"}


def allowed_next_statuses(current_status: str) -> set[str]:
    return STATUS_TRANSITIONS.get(current_status, set())


def direct_close_statuses(current_status: str) -> set[str]:
    """Closed statuses `current_status` can jump straight to (PROJECT_SPEC_PHASE7.md)
    - the standard way to close a case now, with an optional note (unlike reopening
    a closed case, which always requires one). Non-empty for every non-closed
    status; empty once already closed. Only ever offers the two NEW closed
    statuses - see NEW_CLOSE_STATUSES. Kept separate from allowed_next_statuses()
    so the UI can offer this as the primary "close this case" action rather than
    folding it into a generic status dropdown."""
    return NEW_CLOSE_STATUSES if current_status in DIRECT_CLOSE_SOURCE_STATUSES else set()


def _get_active_or_any_station(db: Session, station_id: int, *, field: str) -> Station:
    station = db.get(Station, station_id)
    if station is None:
        raise NotFoundError(f"Station {station_id} does not exist.", field=field)
    return station


def _get_category(db: Session, category_id: int) -> DefectCategory:
    category = db.get(DefectCategory, category_id)
    if category is None:
        raise NotFoundError(f"Defect category {category_id} does not exist.", field="items")
    return category


def list_recent_work_order_numbers(db: Session, *, limit: int = 20) -> list[str]:
    """Distinct work order numbers, most-recently-used first (New Defect form
    autocomplete - PROJECT_SPEC.md entry-speed fix). "Recently used" is by the most
    recent case's detected_at, not created_at, so backdated paper-log entries typed
    in later don't jump ahead of same-day floor entries in the list.
    """
    rows = (
        db.query(DefectCase.work_order_number, func.max(DefectCase.detected_at).label("last_used"))
        .filter(DefectCase.is_deleted.is_(False))
        .group_by(DefectCase.work_order_number)
        .order_by(func.max(DefectCase.detected_at).desc())
        .limit(limit)
        .all()
    )
    return [row.work_order_number for row in rows]


def get_last_case_for_work_order(db: Session, work_order_number: str) -> DefectCase | None:
    """Most recent (by detected_at) non-deleted case on this work order, used to
    pre-fill Found Station when the operator re-types a work order that's already
    had a case logged against it (New Defect form speed fix)."""
    return (
        db.query(DefectCase)
        .filter(
            DefectCase.work_order_number == work_order_number.strip(),
            DefectCase.is_deleted.is_(False),
        )
        .order_by(DefectCase.detected_at.desc())
        .first()
    )


def generate_case_number(db: Session, production_date: dt.date) -> str:
    """DF-YYYYMMDD-NNNN with a 4-digit sequence that resets per production_date.

    Assumption: the sequence resets on production_date (the date the paper log/entry
    is attributed to), not the calendar date the record happens to be typed in on —
    this keeps the date embedded in the case number consistent with the date shown
    everywhere else for that case.
    """
    count = (
        db.query(func.count(DefectCase.id))
        .filter(DefectCase.production_date == production_date)
        .scalar()
        or 0
    )
    sequence = count + 1
    return f"DF-{production_date.strftime('%Y%m%d')}-{sequence:04d}"


def _merge_duplicate_items(
    items: list[dict],
) -> dict[int, dict]:
    """Merge items that share a defect_category_id within one submission.

    Rule 3/6: multiple entries for the same category on the same case must never
    double count as separate defect events, so quantities are summed and notes are
    concatenated instead of creating two DefectItem rows (which the DB's unique
    constraint on (case, category) would reject anyway).
    """
    merged: dict[int, dict] = {}
    for item in items:
        category_id = item["defect_category_id"]
        quantity = item.get("affected_drawer_quantity") or 1
        notes = item.get("notes")
        if category_id in merged:
            merged[category_id]["affected_drawer_quantity"] += quantity
            if notes:
                existing_notes = merged[category_id]["notes"]
                merged[category_id]["notes"] = (
                    f"{existing_notes}; {notes}" if existing_notes else notes
                )
        else:
            merged[category_id] = {
                "defect_category_id": category_id,
                "affected_drawer_quantity": quantity,
                "notes": notes,
            }
    return merged


def create_defect_case(
    db: Session,
    *,
    production_date: dt.date,
    detected_at: dt.datetime,
    work_order_number: str,
    drawer_part_reference: str | None,
    found_station_id: int,
    possible_source_station_id: int | None,
    priority: str,
    items: list[dict],
    disposition: str | None = None,
    resolved_on_the_spot: bool = False,
    instant_close_outcome: str | None = None,
    repair_action: str | None = None,
    root_cause: str | None = None,
    corrective_action: str | None = None,
    notes: str | None = None,
) -> DefectCase:
    """instant_close_outcome: only meaningful when resolved_on_the_spot=True and
    disposition="Rework" - "Repaired" (default) or "Use As Is", see
    INSTANT_CLOSE_OUTCOMES. This is how "defect exists, we're shipping it as is"
    gets recorded now that Use As Is is no longer its own disposition
    (docs/PROJECT_SPEC_PHASE7.md - a DECISION FLAG at the time this shipped)."""
    if not work_order_number or not work_order_number.strip():
        raise ValidationError("Work order number is required.", field="work_order_number")
    if priority not in VALID_PRIORITIES:
        raise ValidationError(f"Priority must be one of {VALID_PRIORITIES}.", field="priority")
    if disposition is not None and disposition not in VALID_DISPOSITIONS:
        raise ValidationError(
            f"Disposition must be one of {VALID_DISPOSITIONS}.", field="disposition"
        )
    if not items:
        raise ValidationError("At least one defect category is required.", field="items")

    # "Fixed immediately?" fast path (PROJECT_SPEC_PHASE7.md) - only Rework has an
    # instant-close path now (Set Aside means "waiting to be worked", the opposite
    # of "already done"), and only trustworthy with a repair_action on record for
    # what was actually done (also feeds the quick repair-action presets on the
    # New Defect form).
    if resolved_on_the_spot and disposition != "Rework":
        raise ValidationError(
            "Resolved on the spot only applies when disposition is Rework.",
            field="resolved_on_the_spot",
        )
    if resolved_on_the_spot and not (repair_action and repair_action.strip()):
        raise ValidationError(
            "Describe what was done to resolve it on the spot.", field="repair_action"
        )
    if instant_close_outcome is not None and not resolved_on_the_spot:
        raise ValidationError(
            "instant_close_outcome only applies when resolved_on_the_spot is true.",
            field="instant_close_outcome",
        )
    effective_outcome = instant_close_outcome or DEFAULT_INSTANT_CLOSE_OUTCOME
    if resolved_on_the_spot and effective_outcome not in INSTANT_CLOSE_OUTCOMES:
        raise ValidationError(
            f"instant_close_outcome must be one of {list(INSTANT_CLOSE_OUTCOMES)}.",
            field="instant_close_outcome",
        )

    _get_active_or_any_station(db, found_station_id, field="found_station_id")
    if possible_source_station_id is not None:
        _get_active_or_any_station(
            db, possible_source_station_id, field="possible_source_station_id"
        )

    merged_items = _merge_duplicate_items(items)
    for category_id in merged_items:
        _get_category(db, category_id)
        if merged_items[category_id]["affected_drawer_quantity"] < 1:
            raise ValidationError("Affected drawer quantity must be at least 1.", field="items")

    # Every non-instant-close case lands on "Open" now, regardless of disposition -
    # there is no more separate "In Rework"/"Waiting" queue status to route into
    # (PROJECT_SPEC_PHASE7.md). The disposition column itself still records WHY a
    # case is open (Rework in progress vs. Set Aside waiting); it just no longer
    # picks a different status for it.
    initial_status = INSTANT_CLOSE_OUTCOMES[effective_outcome] if resolved_on_the_spot else "Open"

    # Phase 7 cost model: one unit of the currently-configured rate, snapshotted at
    # creation, never re-priced later even if the Admin rate changes - see
    # app/services/metrics_service.py compute_case_cost.
    cost_per_drawer_at_time = settings_service.get_cost_per_drawer(db)

    case = DefectCase(
        case_number=generate_case_number(db, production_date),
        production_date=production_date,
        detected_at=detected_at,
        work_order_number=work_order_number.strip(),
        drawer_part_reference=(drawer_part_reference or None),
        found_station_id=found_station_id,
        possible_source_station_id=possible_source_station_id,
        priority=priority,
        status=initial_status,
        disposition=disposition,
        resolved_on_the_spot=resolved_on_the_spot,
        repair_action=repair_action,
        root_cause=root_cause,
        corrective_action=corrective_action,
        notes=notes,
        cost_per_drawer_at_time=cost_per_drawer_at_time,
        closed_at=dt.datetime.now(dt.timezone.utc) if initial_status in CLOSED_STATUSES else None,
    )
    case.items = [
        DefectItem(
            defect_category_id=data["defect_category_id"],
            affected_drawer_quantity=data["affected_drawer_quantity"],
            notes=data["notes"],
        )
        for data in merged_items.values()
    ]
    case.status_history = [
        StatusHistory(
            from_status=None,
            to_status=initial_status,
            note=RESOLVED_ON_THE_SPOT_NOTE if resolved_on_the_spot else "Case created",
        )
    ]

    db.add(case)
    db.commit()
    db.refresh(case)
    return case


def get_case_or_404(db: Session, case_id: int, *, include_deleted: bool = False) -> DefectCase:
    case = db.get(DefectCase, case_id)
    if case is None or (case.is_deleted and not include_deleted):
        raise NotFoundError(f"Defect case {case_id} not found.")
    return case


def get_case_by_number_or_404(db: Session, case_number: str) -> DefectCase:
    case = db.query(DefectCase).filter(DefectCase.case_number == case_number).first()
    if case is None or case.is_deleted:
        raise NotFoundError(f"Defect case {case_number} not found.")
    return case


def add_or_merge_item(
    db: Session,
    case: DefectCase,
    *,
    defect_category_id: int,
    affected_drawer_quantity: int = 1,
    notes: str | None = None,
) -> DefectItem:
    """Add a category to an existing case, merging into an existing item if present.

    This is how rule 6 (reinspection of an unresolved defect updates the existing
    case rather than creating a duplicate) is implemented for the "add a newly
    confirmed category to an existing case" scenario.
    """
    if case.is_deleted:
        raise ValidationError("Cannot modify a deleted case.")
    if affected_drawer_quantity < 1:
        raise ValidationError("Affected drawer quantity must be at least 1.", field="items")
    _get_category(db, defect_category_id)

    existing = next((i for i in case.items if i.defect_category_id == defect_category_id), None)
    if existing is not None:
        existing.affected_drawer_quantity += affected_drawer_quantity
        if notes:
            existing.notes = f"{existing.notes}; {notes}" if existing.notes else notes
        db.commit()
        db.refresh(existing)
        return existing

    item = DefectItem(
        defect_case_id=case.id,
        defect_category_id=defect_category_id,
        affected_drawer_quantity=affected_drawer_quantity,
        notes=notes,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_case_status(
    db: Session,
    case: DefectCase,
    *,
    new_status: str,
    disposition: str | None = None,
    repair_action: str | None = None,
    note: str | None = None,
) -> DefectCase:
    if new_status not in VALID_STATUSES:
        raise ValidationError(f"Status must be one of {VALID_STATUSES}.", field="new_status")
    if disposition is not None and disposition not in VALID_DISPOSITIONS:
        raise ValidationError(
            f"Disposition must be one of {VALID_DISPOSITIONS}.", field="disposition"
        )

    current_status = case.status
    is_reopen = current_status in CLOSED_STATUSES and new_status == "Open"
    is_direct_close = new_status in direct_close_statuses(current_status)

    if is_reopen:
        # Reopening is the one transition that still always requires a note -
        # it's a rare, audit-worthy action (a "done" case coming back), unlike a
        # normal closure. Deliberately NOT relaxed alongside is_direct_close below.
        if not note or not note.strip():
            raise ValidationError(
                "Reopening a closed case requires a note explaining why.", field="note"
            )
    elif not is_direct_close:
        allowed = allowed_next_statuses(current_status)
        if new_status not in allowed:
            raise InvalidTransitionError(
                f"Cannot move from '{current_status}' to '{new_status}'. "
                f"Allowed next statuses: {sorted(allowed) or 'none (terminal status)'}.",
                field="new_status",
            )
    # is_direct_close itself needs no check here: the note is optional supplementary
    # detail for a normal closure (PROJECT_SPEC.md section 3.3) - the repair-action
    # preset captured on the New Defect form (or typed in here) is the primary
    # structured record of what was done, and that's still required where it applies
    # (see create_defect_case's resolved_on_the_spot validation).

    case.status = new_status
    if disposition is not None:
        case.disposition = disposition
    if repair_action is not None:
        case.repair_action = repair_action
    # skipped_recheck is retired (PROJECT_SPEC_PHASE7.md: no recheck status exists
    # anymore) - the column and its historical True/False values stay on old rows,
    # but nothing writes to it for new status changes anymore.
    case.closed_at = dt.datetime.now(dt.timezone.utc) if new_status in CLOSED_STATUSES else None

    db.add(
        StatusHistory(
            defect_case_id=case.id,
            from_status=current_status,
            to_status=new_status,
            note=note,
        )
    )
    db.commit()
    db.refresh(case)
    return case


def soft_delete_case(db: Session, case: DefectCase) -> DefectCase:
    case.is_deleted = True
    db.commit()
    db.refresh(case)
    return case


def bulk_soft_delete_cases(db: Session, ids: list[int]) -> list[DefectCase]:
    """Soft-delete every not-already-deleted case in `ids`. IDs that don't exist
    or are already deleted are silently skipped - the caller only gets back the
    cases it actually changed."""
    cases = (
        db.query(DefectCase).filter(DefectCase.id.in_(ids), DefectCase.is_deleted.is_(False)).all()
    )
    for case in cases:
        case.is_deleted = True
    db.commit()
    for case in cases:
        db.refresh(case)
    return cases


def bulk_restore_cases(db: Session, ids: list[int]) -> list[DefectCase]:
    """Restore every currently-deleted case in `ids`. Same skip-silently rule as
    bulk_soft_delete_cases for IDs that don't exist or aren't deleted."""
    cases = (
        db.query(DefectCase).filter(DefectCase.id.in_(ids), DefectCase.is_deleted.is_(True)).all()
    )
    for case in cases:
        case.is_deleted = False
    db.commit()
    for case in cases:
        db.refresh(case)
    return cases


def check_daily_summary_warnings(
    *,
    drawers_inspected: int,
    drawers_rejected_unique: int,
    drawers_reworked: int,
    drawers_scrapped: int,
) -> list[str]:
    """Soft warnings from PROJECT_SPEC.md section 2.3 — overridable with a note.

    Rework/scrap commonly land on a later date than the original rejection, so a
    single day can legitimately show rework/scrap with zero (or fewer) rejections
    that same day. These are not hard blocks until the pilot confirms date
    attribution across a multi-day repair cycle. drawers_scrapped no longer has a
    form field of its own (Daily Summary "Scrap removal" - see
    docs/PROJECT_SPEC_PHASE4.md) but the column/warning logic is kept for backward
    compatibility with historical and MCP-written rows that still carry a value.
    """
    warnings: list[str] = []
    if drawers_reworked > drawers_rejected_unique:
        warnings.append("Drawers reworked exceeds unique drawers rejected for this date.")
    if drawers_scrapped > drawers_rejected_unique:
        warnings.append("Drawers scrapped exceeds unique drawers rejected for this date.")
    if drawers_reworked + drawers_scrapped > drawers_rejected_unique:
        warnings.append(
            "Drawers reworked plus scrapped exceeds unique drawers rejected for this date."
        )
    return warnings


def validate_daily_summary_input(
    *,
    drawers_inspected: int,
    drawers_rejected_unique: int,
    drawers_reworked: int,
    drawers_scrapped: int,
    notes: str | None,
) -> list[str]:
    """Hard rule + soft-warning-with-required-note check. Returns warnings for display.

    Raises ValidationError if the hard rule is broken, or if soft warnings exist but
    no note was provided to explain the override.
    """
    for label, value in (
        ("drawers_inspected", drawers_inspected),
        ("drawers_rejected_unique", drawers_rejected_unique),
        ("drawers_reworked", drawers_reworked),
        ("drawers_scrapped", drawers_scrapped),
    ):
        if value < 0:
            raise ValidationError(f"{label} cannot be negative.", field=label)

    if drawers_rejected_unique > drawers_inspected:
        raise ValidationError(
            "Unique drawers rejected cannot exceed drawers inspected.",
            field="drawers_rejected_unique",
        )

    warnings = check_daily_summary_warnings(
        drawers_inspected=drawers_inspected,
        drawers_rejected_unique=drawers_rejected_unique,
        drawers_reworked=drawers_reworked,
        drawers_scrapped=drawers_scrapped,
    )
    if warnings and not (notes and notes.strip()):
        raise ValidationError(
            "This entry looks unusual (" + " ".join(warnings) + ") "
            "Add a note explaining why, then save again to confirm it's correct.",
            field="notes",
        )
    return warnings


def upsert_daily_summary(
    db: Session,
    *,
    production_date: dt.date,
    shift: str,
    drawers_inspected: int,
    drawers_rejected_unique: int,
    drawers_reworked: int | None = None,
    drawers_scrapped: int | None = None,
    notes: str | None,
) -> tuple[DailyProductionSummary, list[str]]:
    """drawers_reworked and drawers_scrapped are both Optional because neither is a
    field on the Daily Summary form anymore (drawers_reworked: PROJECT_SPEC_PHASE7.md
    - Rework Rate is now computed from defect cases, not a hand-entered count;
    drawers_scrapped: docs/PROJECT_SPEC_PHASE4.md "Scrap removal"). Passing None for
    either here means "leave whatever was already stored for this date/shift alone"
    (0 for a brand new row), so re-saving a legacy row that does carry a value can
    never silently zero it out. Callers that DO still want to set one explicitly
    (the MCP write tool, direct API/script use) can keep passing an explicit int
    exactly as before.
    """
    row = (
        db.query(DailyProductionSummary)
        .filter(
            DailyProductionSummary.production_date == production_date,
            DailyProductionSummary.shift == shift,
        )
        .first()
    )
    effective_reworked = (
        drawers_reworked
        if drawers_reworked is not None
        else (row.drawers_reworked if row is not None else 0)
    )
    effective_scrapped = (
        drawers_scrapped
        if drawers_scrapped is not None
        else (row.drawers_scrapped if row is not None else 0)
    )

    warnings = validate_daily_summary_input(
        drawers_inspected=drawers_inspected,
        drawers_rejected_unique=drawers_rejected_unique,
        drawers_reworked=effective_reworked,
        drawers_scrapped=effective_scrapped,
        notes=notes,
    )

    if row is None:
        row = DailyProductionSummary(production_date=production_date, shift=shift)
        db.add(row)

    row.drawers_inspected = drawers_inspected
    row.drawers_rejected_unique = drawers_rejected_unique
    row.drawers_reworked = effective_reworked
    row.drawers_scrapped = effective_scrapped
    row.notes = notes
    # Phase 4: snapshot the rate active right now. Never recomputed later even if
    # the Admin rate changes - see app/services/settings_service.py.
    row.cost_per_drawer_at_time = settings_service.get_cost_per_drawer(db)

    db.commit()
    db.refresh(row)
    return row, warnings


def get_daily_summary(
    db: Session, production_date: dt.date, shift: str
) -> DailyProductionSummary | None:
    """The already-saved row for this exact date+shift, if any - used by the Daily
    Summary page to decide whether to show a saved entry as-is or pre-fill a new
    one from suggested_daily_counts() (never both - see that function's docstring)."""
    return (
        db.query(DailyProductionSummary)
        .filter(
            DailyProductionSummary.production_date == production_date,
            DailyProductionSummary.shift == shift,
        )
        .first()
    )


def suggested_daily_counts(db: Session, production_date: dt.date) -> dict:
    """Suggested "Unique Drawers Rejected" count for the Daily Production Summary
    form, computed from real DefectCase data instead of typed by hand
    (docs/PROJECT_SPEC_PHASE4.md "Scrap removal" / auto-calculation).

    Unique drawers rejected = count of distinct, non-deleted DefectCase rows for
    this production_date, regardless of disposition. One DefectCase already IS
    one defective drawer no matter how many DefectItem categories are on it
    (PROJECT_SPEC.md section 2), so counting distinct cases - not items - is the
    same dedup rule used everywhere else in the app (e.g.
    app/routers/reports.py _distinct_cases, the section 3.3 KPIs); it already
    guarantees a drawer flagged under two categories on one case is counted once.

    No longer also suggests a reworked count (PROJECT_SPEC_PHASE7.md): "Drawers
    reworked" left the Daily Summary form entirely - Rework Rate is now computed
    straight from cases with disposition "Rework" (see
    app/services/metrics_service.py / app/routers/reports.py get_summary), not a
    suggested-then-typed number.

    LIMITATION: DefectCase has no shift field, only production_date, so this
    suggestion is computed at the whole-day level and is identical no matter which
    shift the Daily Summary form is being filled out for. If a plant ever runs two
    shifts against the same production_date, both shifts' forms will suggest the
    same day-level count - matching this by shift would require adding a shift
    field to DefectCase, which is out of scope here.
    """
    case_count = (
        db.query(func.count(DefectCase.id))
        .filter(
            DefectCase.production_date == production_date,
            DefectCase.is_deleted.is_(False),
        )
        .scalar()
        or 0
    )
    return {
        "production_date": production_date,
        "defect_case_count": case_count,
        "suggested_drawers_rejected_unique": case_count,
    }
