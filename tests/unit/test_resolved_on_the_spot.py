"""PROJECT_SPEC_PHASE7.md: immediate resolution is still the standard path for
Rework. Set Aside (replacing the old Use As Is / Hold / Scrap) always queues as
Open - "Close Directly" from the queue is the only way it ever closes.

- "Resolved on the spot" at entry (create_defect_case resolved_on_the_spot=True) -
  only valid with disposition "Rework"; instant_close_outcome picks which of the
  two closed statuses it lands on ("Repaired" default, or "Use As Is" - the
  DECISION-FLAGGED entry point for recording "shipping as is" without a separate
  disposition).
- "Close Directly" from the queue (update_case_status any non-closed status ->
  Closed-* directly, with an optional note) - the standard closing action.
  Reopening a closed case is the one exception still requiring a note.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.errors import InvalidTransitionError, ValidationError
from app.services import metrics_service
from app.services.defect_service import (
    VALID_DISPOSITIONS,
    create_defect_case,
    direct_close_statuses,
    update_case_status,
)


def _make_case(db_session, stations, categories, today, **overrides):
    payload = dict(
        production_date=today,
        detected_at=dt.datetime(2026, 7, 24, 9, 0, tzinfo=dt.timezone.utc),
        work_order_number="WO-3001",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[
            {
                "defect_category_id": categories["Sanding / Surface"].id,
                "affected_drawer_quantity": 1,
            }
        ],
    )
    payload.update(overrides)
    return create_defect_case(db_session, **payload)


# ---------------------------------------------------------------------------
# Rework primary / Set Aside secondary (section 3.2 / PROJECT_SPEC_PHASE7.md)
# ---------------------------------------------------------------------------


def test_rework_is_the_default_primary_disposition():
    """VALID_DISPOSITIONS order drives the New Defect form's button order and
    prominence - Rework must be first (big, pre-selected)."""
    assert VALID_DISPOSITIONS == ["Rework", "Set Aside"]


# ---------------------------------------------------------------------------
# Resolved on the spot: only Rework has an instant-close path, choosing between
# the two closed outcomes.
# ---------------------------------------------------------------------------


def test_instant_close_default_outcome_is_repaired(db_session, stations, categories, today):
    case = _make_case(
        db_session,
        stations,
        categories,
        today,
        disposition="Rework",
        resolved_on_the_spot=True,
        repair_action="Resanded",
    )
    assert case.status == "Closed - Repaired"
    assert case.resolved_on_the_spot is True
    assert case.skipped_recheck is False
    assert case.closed_at is not None
    assert case.repair_action == "Resanded"


def test_instant_close_use_as_is_outcome(db_session, stations, categories, today):
    case = _make_case(
        db_session,
        stations,
        categories,
        today,
        disposition="Rework",
        resolved_on_the_spot=True,
        instant_close_outcome="Use As Is",
        repair_action="Buyer accepted as-is",
    )
    assert case.status == "Closed - Use As Is"
    assert case.closed_at is not None


def test_instant_close_writes_resolved_on_the_spot_audit_note(
    db_session, stations, categories, today
):
    case = _make_case(
        db_session,
        stations,
        categories,
        today,
        disposition="Rework",
        resolved_on_the_spot=True,
        repair_action="Replaced part",
    )
    assert len(case.status_history) == 1
    entry = case.status_history[0]
    assert entry.from_status is None
    assert entry.to_status == "Closed - Repaired"
    assert entry.note == "Resolved on the spot at entry"


def test_normal_entry_still_writes_case_created_note(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today)
    assert case.status_history[0].note == "Case created"
    assert case.resolved_on_the_spot is False


def test_resolved_on_the_spot_rejects_set_aside(db_session, stations, categories, today):
    with pytest.raises(ValidationError) as exc:
        _make_case(
            db_session,
            stations,
            categories,
            today,
            disposition="Set Aside",
            resolved_on_the_spot=True,
            repair_action="Resanded",
        )
    assert exc.value.field == "resolved_on_the_spot"


def test_resolved_on_the_spot_rejects_no_disposition(db_session, stations, categories, today):
    with pytest.raises(ValidationError) as exc:
        _make_case(
            db_session,
            stations,
            categories,
            today,
            disposition=None,
            resolved_on_the_spot=True,
            repair_action="Resanded",
        )
    assert exc.value.field == "resolved_on_the_spot"


def test_resolved_on_the_spot_requires_repair_action(db_session, stations, categories, today):
    with pytest.raises(ValidationError) as exc:
        _make_case(
            db_session,
            stations,
            categories,
            today,
            disposition="Rework",
            resolved_on_the_spot=True,
        )
    assert exc.value.field == "repair_action"


def test_resolved_on_the_spot_requires_non_blank_repair_action(
    db_session, stations, categories, today
):
    with pytest.raises(ValidationError) as exc:
        _make_case(
            db_session,
            stations,
            categories,
            today,
            disposition="Rework",
            resolved_on_the_spot=True,
            repair_action="   ",
        )
    assert exc.value.field == "repair_action"


def test_instant_close_outcome_rejected_without_resolved_on_the_spot(
    db_session, stations, categories, today
):
    with pytest.raises(ValidationError) as exc:
        _make_case(
            db_session,
            stations,
            categories,
            today,
            disposition="Rework",
            instant_close_outcome="Use As Is",
        )
    assert exc.value.field == "instant_close_outcome"


def test_invalid_instant_close_outcome_rejected(db_session, stations, categories, today):
    with pytest.raises(ValidationError) as exc:
        _make_case(
            db_session,
            stations,
            categories,
            today,
            disposition="Rework",
            resolved_on_the_spot=True,
            instant_close_outcome="Scrapped",
            repair_action="Resanded",
        )
    assert exc.value.field == "instant_close_outcome"


# ---------------------------------------------------------------------------
# Leaving a case open (resolved_on_the_spot=False): every non-instant case
# lands on "Open" now, regardless of disposition - there is no more separate
# "In Rework"/"Waiting" queue status to route into.
# ---------------------------------------------------------------------------


def test_disposition_rework_left_open_is_open(db_session, stations, categories, today):
    """The New Defect form's "Not resolved yet - leave this case open" checkbox,
    with Rework chosen, lands the case on Open now (In Rework is retired)."""
    case = _make_case(db_session, stations, categories, today, disposition="Rework")
    assert case.status == "Open"
    assert case.resolved_on_the_spot is False
    assert case.closed_at is None


def test_disposition_set_aside_is_always_open(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today, disposition="Set Aside")
    assert case.status == "Open"
    assert case.closed_at is None


def test_no_disposition_is_open(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today)
    assert case.status == "Open"


# ---------------------------------------------------------------------------
# Close Directly from the Rework Queue - the standard closing action, available
# from every non-closed status.
# ---------------------------------------------------------------------------


def test_direct_close_statuses_available_from_every_non_closed_status(
    db_session, stations, categories, today
):
    new_closed = {"Closed - Repaired", "Closed - Use As Is"}
    for status in ["Open", "In Rework", "Waiting", "Ready for QC Recheck"]:
        assert direct_close_statuses(status) == new_closed
    for status in new_closed | {"Closed - Scrapped"}:
        assert direct_close_statuses(status) == set()


def test_direct_close_from_open_succeeds_with_note(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today)
    assert case.status == "Open"

    updated = update_case_status(
        db_session, case, new_status="Closed - Repaired", note="Fixed immediately, no queue needed."
    )
    assert updated.status == "Closed - Repaired"


def test_direct_close_succeeds_with_no_note(db_session, stations, categories, today):
    """The note is optional supplementary detail for a normal closure - the
    repair-action preset is the primary structured record of what was done, so a
    close must not be blocked on typing anything else here."""
    case = _make_case(db_session, stations, categories, today, disposition="Rework")
    updated = update_case_status(db_session, case, new_status="Closed - Repaired")
    assert updated.status == "Closed - Repaired"
    assert updated.status_history[-1].note is None


def test_direct_close_succeeds_with_blank_note(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today, disposition="Rework")
    updated = update_case_status(db_session, case, new_status="Closed - Repaired", note="   ")
    assert updated.status == "Closed - Repaired"


def test_reopen_a_closed_case_still_requires_a_note(db_session, stations, categories, today):
    """Reopening is the one transition NOT relaxed by the note-optional change -
    it's rare and audit-worthy enough to still require an explanation."""
    case = _make_case(db_session, stations, categories, today, disposition="Rework")
    closed = update_case_status(db_session, case, new_status="Closed - Repaired")

    with pytest.raises(ValidationError) as exc:
        update_case_status(db_session, closed, new_status="Open")
    assert exc.value.field == "note"

    reopened = update_case_status(
        db_session, closed, new_status="Open", note="QC recheck found the repair failed."
    )
    assert reopened.status == "Open"


def test_still_genuinely_invalid_transition_raises(db_session, stations, categories, today):
    """Open -> Ready for QC Recheck is not a direct-close target (that status is
    retired) and not in STATUS_TRANSITIONS, so it's rejected outright before even
    reaching the InvalidTransitionError path - VALID_STATUSES itself rejects it."""
    case = _make_case(db_session, stations, categories, today)
    assert case.status == "Open"
    with pytest.raises(ValidationError):
        update_case_status(db_session, case, new_status="Ready for QC Recheck")


def test_open_to_open_is_rejected_as_a_no_op_transition(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today)
    with pytest.raises(InvalidTransitionError):
        update_case_status(db_session, case, new_status="Open")


# ---------------------------------------------------------------------------
# % Resolved On The Spot (PROJECT_SPEC.md section 3.3) - kept, definition
# unchanged by Phase 7.
# ---------------------------------------------------------------------------


def test_compute_resolved_on_the_spot_rate():
    assert (
        metrics_service.compute_resolved_on_the_spot_rate(
            total_cases=20, resolved_on_the_spot_count=5
        )
        == 25.0
    )
    assert (
        metrics_service.compute_resolved_on_the_spot_rate(
            total_cases=0, resolved_on_the_spot_count=0
        )
        is None
    )
