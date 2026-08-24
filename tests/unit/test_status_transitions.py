"""PROJECT_SPEC_PHASE7.md: the simplified three-status transition map
(Open / Closed - Repaired / Closed - Use As Is) and reopen rules.

Retired statuses (In Rework, Waiting, Ready for QC Recheck, Closed - Scrapped)
still exist as historical data - see tests/unit/test_phase7_migration.py for the
migration that moves currently-open legacy cases, and the "legacy status still
behaves sanely if seen again" coverage below.
"""

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


# ---------------------------------------------------------------------------
# Direct close is the ONLY way out of Open now - no intermediate open states.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("new_status", ["Closed - Repaired", "Closed - Use As Is"])
def test_open_closes_directly_with_no_note_required(
    db_session, stations, categories, today, new_status
):
    case = _make_case(db_session, stations, categories, today)
    updated = update_case_status(db_session, case, new_status=new_status)
    assert updated.status == new_status
    assert updated.closed_at is not None


@pytest.mark.parametrize(
    "new_status",
    ["In Rework", "Waiting", "Ready for QC Recheck", "Closed - Scrapped"],
)
def test_retired_statuses_are_rejected_as_a_new_write(
    db_session, stations, categories, today, new_status
):
    """Retired for new entry (PROJECT_SPEC_PHASE7.md) - the API must reject any
    attempt to SET a case to one of these, even though they remain valid stored
    values on historical rows."""
    case = _make_case(db_session, stations, categories, today)
    with pytest.raises(ValidationError) as exc:
        update_case_status(db_session, case, new_status=new_status)
    assert exc.value.field == "new_status"


def test_disposition_set_aside_is_accepted(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today)
    updated = update_case_status(
        db_session, case, new_status="Closed - Repaired", disposition="Set Aside"
    )
    assert updated.disposition == "Set Aside"


@pytest.mark.parametrize("retired", ["Use As Is", "Hold", "Scrap"])
def test_retired_dispositions_are_rejected_as_a_new_write(
    db_session, stations, categories, today, retired
):
    case = _make_case(db_session, stations, categories, today)
    with pytest.raises(ValidationError) as exc:
        update_case_status(db_session, case, new_status="Closed - Repaired", disposition=retired)
    assert exc.value.field == "disposition"


# ---------------------------------------------------------------------------
# Terminal statuses have no next status except reopen.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("closed_status", ["Closed - Repaired", "Closed - Use As Is"])
def test_closed_statuses_reject_every_transition_except_reopen(
    db_session, stations, categories, today, closed_status
):
    case = _make_case(db_session, stations, categories, today)
    update_case_status(db_session, case, new_status=closed_status)
    with pytest.raises(InvalidTransitionError):
        update_case_status(db_session, case, new_status="Closed - Use As Is")


def test_reopen_closed_case_requires_note(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today)
    update_case_status(db_session, case, new_status="Closed - Repaired")

    with pytest.raises(ValidationError) as exc:
        update_case_status(db_session, case, new_status="Open")
    assert exc.value.field == "note"

    reopened = update_case_status(
        db_session, case, new_status="Open", note="QC recheck found the repair failed."
    )
    assert reopened.status == "Open"
    assert reopened.status_history[-1].note == "QC recheck found the repair failed."
    assert reopened.closed_at is None


def test_status_change_writes_status_history(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today)
    update_case_status(db_session, case, new_status="Closed - Repaired", note="Fixed at the bench")
    db_session.refresh(case)
    assert [h.to_status for h in case.status_history] == ["Open", "Closed - Repaired"]
    assert case.status_history[-1].from_status == "Open"


def test_update_case_status_no_longer_writes_skipped_recheck(
    db_session, stations, categories, today
):
    """skipped_recheck is retired (no recheck status exists anymore) - the column
    stays on the model for historical rows, but new status changes must not
    write to it."""
    case = _make_case(db_session, stations, categories, today)
    assert case.skipped_recheck is False
    updated = update_case_status(db_session, case, new_status="Closed - Repaired")
    assert updated.skipped_recheck is False


# ---------------------------------------------------------------------------
# A legacy status still on a case (e.g. seeded directly, or surviving from
# before the Phase 7 migration ran) must still behave sanely: it's a valid
# direct-close SOURCE (defensive - the migration should mean none exist for a
# non-closed case in practice), but never a valid target.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("legacy_status", ["In Rework", "Waiting", "Ready for QC Recheck"])
def test_a_case_still_in_a_legacy_open_status_can_still_be_closed_directly(
    db_session, stations, categories, today, legacy_status
):
    case = _make_case(db_session, stations, categories, today)
    case.status = legacy_status
    db_session.commit()

    updated = update_case_status(db_session, case, new_status="Closed - Repaired")
    assert updated.status == "Closed - Repaired"


def test_a_historically_scrapped_case_can_still_be_reopened(
    db_session, stations, categories, today
):
    case = _make_case(db_session, stations, categories, today)
    case.status = "Closed - Scrapped"
    db_session.commit()

    with pytest.raises(ValidationError):
        update_case_status(db_session, case, new_status="Open")

    reopened = update_case_status(
        db_session, case, new_status="Open", note="Reconsidered - reworking instead."
    )
    assert reopened.status == "Open"
