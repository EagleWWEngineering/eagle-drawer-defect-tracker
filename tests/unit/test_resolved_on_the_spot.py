"""PROJECT_SPEC.md section 3.3: immediate resolution is the standard path,
queued/Hold is the exception.

- "Resolved on the spot" at entry (create_defect_case resolved_on_the_spot=True) -
  the New Defect form's default flow.
- "Close Directly" from the queue (update_case_status any non-closed status ->
  Closed-* directly, with an optional note) - the standard closing action,
  available from every non-closed status, not just In Rework. Reopening a closed
  case is the one exception still requiring a note.

Plus the metrics_service pure functions behind the two new KPIs, and the
Rework-is-primary/Scrap-is-secondary disposition ordering (section 3.2).
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
# Rework primary / Scrap secondary (section 3.2)
# ---------------------------------------------------------------------------


def test_rework_is_the_default_primary_disposition():
    """VALID_DISPOSITIONS order drives the New Defect form's button order and
    prominence - Rework must be first (big, pre-selected) and Scrap last (tucked
    behind "More options...")."""
    assert VALID_DISPOSITIONS[0] == "Rework"
    assert VALID_DISPOSITIONS[-1] == "Scrap"


# ---------------------------------------------------------------------------
# Resolved on the spot (all three eligible dispositions, including the now-rare
# Scrap - it still has to work end to end when explicitly chosen)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "disposition,expected_status",
    [
        ("Rework", "Closed - Repaired"),
        ("Scrap", "Closed - Scrapped"),
        ("Use As Is", "Closed - Use As Is"),
    ],
)
def test_instant_close_all_three_dispositions(
    db_session, stations, categories, today, disposition, expected_status
):
    case = _make_case(
        db_session,
        stations,
        categories,
        today,
        disposition=disposition,
        resolved_on_the_spot=True,
        repair_action="Resanded",
    )
    assert case.status == expected_status
    assert case.resolved_on_the_spot is True
    assert case.skipped_recheck is False
    assert case.closed_at is not None
    assert case.repair_action == "Resanded"


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


@pytest.mark.parametrize("disposition", [None, "Hold"])
def test_resolved_on_the_spot_rejects_ineligible_disposition(
    db_session, stations, categories, today, disposition
):
    with pytest.raises(ValidationError) as exc:
        _make_case(
            db_session,
            stations,
            categories,
            today,
            disposition=disposition,
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
            disposition="Scrap",
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
            disposition="Scrap",
            resolved_on_the_spot=True,
            repair_action="   ",
        )
    assert exc.value.field == "repair_action"


# ---------------------------------------------------------------------------
# Leaving a case open (resolved_on_the_spot=False) - the secondary/exception path.
# Rework/Hold routing unchanged, Scrap/Use As Is deliberately changed from
# "auto-close" to "Open" (PROJECT_SPEC.md section 3.2).
# ---------------------------------------------------------------------------


def test_disposition_rework_left_open_is_in_rework(db_session, stations, categories, today):
    """The New Defect form's "Not resolved yet - leave this case open" checkbox,
    with Rework chosen, must still land the case in the queue as In Rework."""
    case = _make_case(db_session, stations, categories, today, disposition="Rework")
    assert case.status == "In Rework"
    assert case.resolved_on_the_spot is False
    assert case.closed_at is None


def test_disposition_hold_is_always_left_open_as_waiting(db_session, stations, categories, today):
    """Hold has no "resolved" concept of its own - picking it always leaves the
    case open as Waiting, the same effect as the leave-open checkbox, without
    needing the checkbox at all."""
    case = _make_case(db_session, stations, categories, today, disposition="Hold")
    assert case.status == "Waiting"
    assert case.resolved_on_the_spot is False


@pytest.mark.parametrize("disposition", ["Scrap", "Use As Is"])
def test_disposition_scrap_and_use_as_is_left_open_stay_open(
    db_session, stations, categories, today, disposition
):
    """Deliberate behavior change (PROJECT_SPEC.md section 3.2): these used to
    auto-close at creation with no "was it actually done?" gate. Now they queue as
    Open, same as an undecided case, until either resolved_on_the_spot=True at entry
    or an explicit later Close Directly action closes them."""
    case = _make_case(db_session, stations, categories, today, disposition=disposition)
    assert case.status == "Open"
    assert case.closed_at is None


def test_no_disposition_is_open(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today)
    assert case.status == "Open"


# ---------------------------------------------------------------------------
# Close Directly from the Rework Queue - the standard closing action, available
# from every non-closed status now, not a narrow "skip recheck" exception.
# ---------------------------------------------------------------------------


def test_direct_close_statuses_available_from_every_non_closed_status(
    db_session, stations, categories, today
):
    all_three = {"Closed - Repaired", "Closed - Scrapped", "Closed - Use As Is"}
    for status in ["Open", "In Rework", "Waiting", "Ready for QC Recheck"]:
        assert direct_close_statuses(status) == all_three
    for status in all_three:
        assert direct_close_statuses(status) == set()


@pytest.mark.parametrize(
    "target_status", ["Closed - Repaired", "Closed - Scrapped", "Closed - Use As Is"]
)
def test_direct_close_from_in_rework_succeeds_with_note(
    db_session, stations, categories, today, target_status
):
    case = _make_case(db_session, stations, categories, today, disposition="Rework")
    assert case.status == "In Rework"

    updated = update_case_status(
        db_session,
        case,
        new_status=target_status,
        note="Confirmed repaired at the bench, no recheck needed.",
    )
    assert updated.status == target_status
    assert updated.skipped_recheck is True
    assert updated.closed_at is not None
    assert updated.status_history[-1].note == "Confirmed repaired at the bench, no recheck needed."


def test_direct_close_from_open_succeeds_with_note(db_session, stations, categories, today):
    """Open never reached In Rework, so this counts as a direct close but is
    correctly excluded from the "% Queued Rework Closed Without Recheck"
    denominator (see reports.py _reached_in_rework_case_ids)."""
    case = _make_case(db_session, stations, categories, today)
    assert case.status == "Open"

    updated = update_case_status(
        db_session, case, new_status="Closed - Repaired", note="Fixed immediately, no queue needed."
    )
    assert updated.status == "Closed - Repaired"
    assert updated.skipped_recheck is True


def test_direct_close_from_waiting_succeeds_with_note(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today, disposition="Hold")
    assert case.status == "Waiting"

    updated = update_case_status(
        db_session, case, new_status="Closed - Use As Is", note="Decided to use as is."
    )
    assert updated.status == "Closed - Use As Is"
    assert updated.skipped_recheck is True


def test_direct_close_succeeds_with_no_note(db_session, stations, categories, today):
    """The note is optional supplementary detail for a normal closure - the
    repair-action preset is the primary structured record of what was done, so a
    close must not be blocked on typing anything else here."""
    case = _make_case(db_session, stations, categories, today, disposition="Rework")
    updated = update_case_status(db_session, case, new_status="Closed - Repaired")
    assert updated.status == "Closed - Repaired"
    assert updated.skipped_recheck is True
    assert updated.status_history[-1].note is None


def test_direct_close_succeeds_with_blank_note(db_session, stations, categories, today):
    case = _make_case(db_session, stations, categories, today, disposition="Rework")
    updated = update_case_status(db_session, case, new_status="Closed - Repaired", note="   ")
    assert updated.status == "Closed - Repaired"


def test_legacy_recheck_path_still_works_end_to_end(db_session, stations, categories, today):
    """The full In Rework -> Ready for QC Recheck -> Closed path must stay valid
    for backward compatibility, even though nothing in the UI presents "Ready for
    QC Recheck" as an expected step anymore. The note is optional here too, same as
    every other direct close, but this one must NOT count as "skipped recheck"
    since it genuinely was rechecked."""
    case = _make_case(db_session, stations, categories, today, disposition="Rework")
    update_case_status(db_session, case, new_status="Ready for QC Recheck")
    updated = update_case_status(db_session, case, new_status="Closed - Repaired")
    assert updated.status == "Closed - Repaired"
    assert updated.skipped_recheck is False


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
    """Open -> Ready for QC Recheck is not a direct-close target and not in
    STATUS_TRANSITIONS, so it's still rejected as an invalid transition, not
    treated as a close needing a note."""
    case = _make_case(db_session, stations, categories, today)
    assert case.status == "Open"
    with pytest.raises(InvalidTransitionError):
        update_case_status(db_session, case, new_status="Ready for QC Recheck")


# ---------------------------------------------------------------------------
# metrics_service pure functions behind the two new KPIs
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


def test_compute_skip_recheck_rate():
    assert (
        metrics_service.compute_skip_recheck_rate(queued_rework_count=8, skipped_recheck_count=2)
        == 25.0
    )
    assert (
        metrics_service.compute_skip_recheck_rate(queued_rework_count=0, skipped_recheck_count=0)
        is None
    )
