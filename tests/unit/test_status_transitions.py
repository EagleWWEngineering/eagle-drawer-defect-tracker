"""PROJECT_SPEC.md section 3.1/3.2: allowed status transitions and reopen rules."""

from __future__ import annotations

import datetime as dt

import pytest

from app.errors import InvalidTransitionError, ValidationError
from app.services.defect_service import create_defect_case, update_case_status


def _make_case(db_session, stations, categories, today, priority="Normal"):
    return create_defect_case(
        db_session,
        production_date=today,
        detected_at=dt.datetime(2026, 7, 24, 9, 0, tzinfo=dt.timezone.utc),
        work_order_number="WO-2001",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority=priority,
        items=[
            {
                "defect_category_id": categories["Sanding / Surface"].id,
                "affected_drawer_quantity": 1,
            }
        ],
    )


@pytest.mark.parametrize(
    "current,new_status",
    [
        ("Open", "In Rework"),
        ("Open", "Waiting"),
        ("In Rework", "Ready for QC Recheck"),
        ("In Rework", "Waiting"),
        ("Waiting", "In Rework"),
        ("Ready for QC Recheck", "In Rework"),
        # NOTE: every "-> Closed - *" transition used to be listed here too (some
        # requiring no note, e.g. Open -> Closed - Scrapped). PROJECT_SPEC.md
        # section 3.3 made closing directly - from ANY non-closed status, to ANY
        # of the 3 closed statuses - the standard action instead, always gated on
        # a required note. See tests/unit/test_resolved_on_the_spot.py for that
        # coverage (with-note-succeeds / without-note-fails, from every source
        # status).
    ],
)
def test_allowed_transitions_succeed(db_session, stations, categories, today, current, new_status):
    case = _make_case(db_session, stations, categories, today)
    case.status = current
    db_session.commit()

    updated = update_case_status(db_session, case, new_status=new_status)
    assert updated.status == new_status


@pytest.mark.parametrize(
    "current,new_status",
    [
        ("Open", "Ready for QC Recheck"),
        ("Closed - Repaired", "In Rework"),
        ("Closed - Scrapped", "Waiting"),
        # NOTE: Open/Waiting/Ready for QC Recheck -> a Closed status used to be
        # disallowed here for some source/target combinations. They're all direct
        # closes now (require a note instead of being flatly rejected) - see
        # tests/unit/test_resolved_on_the_spot.py.
    ],
)
def test_disallowed_transitions_raise(db_session, stations, categories, today, current, new_status):
    case = _make_case(db_session, stations, categories, today)
    case.status = current
    db_session.commit()

    with pytest.raises(InvalidTransitionError):
        update_case_status(db_session, case, new_status=new_status)


def test_reopen_closed_case_requires_note(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today)
    case.status = "Closed - Repaired"
    db_session.commit()

    with pytest.raises(ValidationError):
        update_case_status(db_session, case, new_status="Open")

    reopened = update_case_status(
        db_session, case, new_status="Open", note="QC recheck found the repair failed."
    )
    assert reopened.status == "Open"
    assert reopened.status_history[-1].note == "QC recheck found the repair failed."


def test_disposition_scrap_closes_case(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today)
    updated = update_case_status(
        db_session,
        case,
        new_status="Closed - Scrapped",
        disposition="Scrap",
        note="Confirmed scrapped - not repairable.",
    )
    assert updated.status == "Closed - Scrapped"
    assert updated.disposition == "Scrap"
    assert updated.closed_at is not None


def test_status_change_writes_status_history(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today)
    update_case_status(db_session, case, new_status="In Rework", note="Starting rework")
    db_session.refresh(case)
    assert [h.to_status for h in case.status_history] == ["Open", "In Rework"]
    assert case.status_history[-1].from_status == "Open"
