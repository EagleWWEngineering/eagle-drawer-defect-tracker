"""Alembic migration test for PROJECT_SPEC_PHASE7.md Change 3: seed a database
with every retired status/disposition combination (mixed with untouched control
rows), run `alembic upgrade head`, and assert:

  - every currently-open case in a retired status (In Rework/Waiting/Ready for
    QC Recheck) moved to "Open", with a real status_history row and an
    audit_log row;
  - an open case with a retired disposition (Hold/Scrap/Use As Is) had its
    disposition remapped to "Set Aside", also logged;
  - closed cases (including ones with a retired disposition like "Closed -
    Repaired"/"Hold") are BYTE-IDENTICAL - not their status, not their
    disposition, nothing - and got no migration-tagged log rows at all;
  - an already-compliant Open case with no retired values is left alone.

Runs the real migrations via `python -m alembic` in a subprocess against a
throwaway SQLite file, rather than the in-process `db_session` fixture, because
alembic/env.py re-reads DATABASE_URL from the environment at import time - a
subprocess with its own environment is the cleanest way to point a real
`alembic upgrade` run at a temporary database.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# The revision right before the Phase 7 data migration - schema-complete
# (including the new defect_cases.cost_per_drawer_at_time column) but the
# retired-status/disposition rows haven't been touched yet.
PRE_MIGRATION_REVISION = "3d8532f3a9ec"

MIGRATION_MESSAGE_PREFIX = "Phase 7 migration:"


def _run_alembic(args: list[str], env: dict) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert (
        result.returncode == 0
    ), f"alembic {' '.join(args)} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"


def _seed_case(
    conn: sqlite3.Connection,
    *,
    case_number: str,
    status: str,
    disposition: str | None,
    station_id: int,
) -> int:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO defect_cases (
            case_number, production_date, detected_at, work_order_number,
            found_station_id, priority, status, disposition,
            resolved_on_the_spot, skipped_recheck, created_at, updated_at, is_deleted
        ) VALUES (?, ?, ?, ?, ?, 'Normal', ?, ?, 0, 0, ?, ?, 0)
        """,
        (
            case_number,
            "2026-08-01",
            now,
            "WO-MIGRATION-TEST",
            station_id,
            status,
            disposition,
            now,
            now,
        ),
    )
    return cur.lastrowid


def test_phase7_migration_moves_open_legacy_cases_and_preserves_closed_ones(tmp_path):
    db_path = tmp_path / "phase7_migration_test.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"

    # 1. Build the schema up to (but not including) the data migration.
    _run_alembic(["upgrade", PRE_MIGRATION_REVISION], env)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    station_id = conn.execute(
        "INSERT INTO stations (name, active, sort_order, created_at, updated_at) "
        "VALUES ('Test Station', 1, 0, ?, ?)",
        (now, now),
    ).lastrowid

    # Every retired-status case (should move to Open + get a status_history/
    # audit_log row).
    in_rework_id = _seed_case(
        conn,
        case_number="DF-MIG-0001",
        status="In Rework",
        disposition="Rework",
        station_id=station_id,
    )
    waiting_id = _seed_case(
        conn,
        case_number="DF-MIG-0002",
        status="Waiting",
        disposition="Hold",
        station_id=station_id,
    )
    recheck_id = _seed_case(
        conn,
        case_number="DF-MIG-0003",
        status="Ready for QC Recheck",
        disposition="Rework",
        station_id=station_id,
    )
    # Already-Open cases with a retired disposition (only the disposition changes).
    open_scrap_id = _seed_case(
        conn,
        case_number="DF-MIG-0004",
        status="Open",
        disposition="Scrap",
        station_id=station_id,
    )
    open_use_as_is_id = _seed_case(
        conn,
        case_number="DF-MIG-0005",
        status="Open",
        disposition="Use As Is",
        station_id=station_id,
    )
    # Already-compliant Open case - nothing to do, must be left alone.
    open_control_id = _seed_case(
        conn,
        case_number="DF-MIG-0006",
        status="Open",
        disposition=None,
        station_id=station_id,
    )
    # Closed cases, including ones with a retired disposition - NEVER touched.
    closed_repaired_hold_id = _seed_case(
        conn,
        case_number="DF-MIG-0007",
        status="Closed - Repaired",
        disposition="Hold",
        station_id=station_id,
    )
    closed_scrapped_id = _seed_case(
        conn,
        case_number="DF-MIG-0008",
        status="Closed - Scrapped",
        disposition="Scrap",
        station_id=station_id,
    )
    conn.commit()

    before_rows = {
        row[0]: dict(zip(("status", "disposition", "updated_at"), row[1:], strict=False))
        for row in conn.execute(
            "SELECT id, status, disposition, updated_at FROM defect_cases"
        ).fetchall()
    }
    conn.close()

    # 2. Run the real data migration.
    _run_alembic(["upgrade", "head"], env)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    def _case(case_id: int) -> sqlite3.Row:
        return conn.execute("SELECT * FROM defect_cases WHERE id = ?", (case_id,)).fetchone()

    def _history_notes(case_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM status_history WHERE defect_case_id = ?", (case_id,)
        ).fetchall()

    def _audit_rows(case_number: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM audit_log WHERE entity_type = 'DefectCase' AND entity_id = ? "
            "AND message LIKE ?",
            (case_number, f"{MIGRATION_MESSAGE_PREFIX}%"),
        ).fetchall()

    # --- Retired statuses moved to Open, with history + audit rows ---
    for case_id, case_number, old_status in [
        (in_rework_id, "DF-MIG-0001", "In Rework"),
        (waiting_id, "DF-MIG-0002", "Waiting"),
        (recheck_id, "DF-MIG-0003", "Ready for QC Recheck"),
    ]:
        row = _case(case_id)
        assert row["status"] == "Open"
        history = _history_notes(case_id)
        assert len(history) == 1
        assert history[0]["from_status"] == old_status
        assert history[0]["to_status"] == "Open"
        assert history[0]["note"]
        audit_rows = _audit_rows(case_number)
        assert len(audit_rows) == 1

    # Disposition preserved for the pure-status migration of case 1 and 3 (still
    # "Rework", not retired); case 2's disposition ("Hold") IS retired and must
    # also have been remapped.
    assert _case(in_rework_id)["disposition"] == "Rework"
    assert _case(waiting_id)["disposition"] == "Set Aside"
    assert _case(recheck_id)["disposition"] == "Rework"

    # --- Already-Open cases: only disposition changes, status stays Open, no
    # status_history row (no status transition happened) but still an audit row.
    for case_id, case_number in [
        (open_scrap_id, "DF-MIG-0004"),
        (open_use_as_is_id, "DF-MIG-0005"),
    ]:
        row = _case(case_id)
        assert row["status"] == "Open"
        assert row["disposition"] == "Set Aside"
        assert _history_notes(case_id) == []
        assert len(_audit_rows(case_number)) == 1

    # --- Already-compliant Open case: completely untouched, no log rows at all.
    control_row = _case(open_control_id)
    assert control_row["status"] == "Open"
    assert control_row["disposition"] is None
    assert control_row["updated_at"] == before_rows[open_control_id]["updated_at"]
    assert _history_notes(open_control_id) == []
    assert _audit_rows("DF-MIG-0006") == []

    # --- Closed cases: byte-identical, no exceptions, even with a retired
    # disposition already on them.
    for case_id, case_number in [
        (closed_repaired_hold_id, "DF-MIG-0007"),
        (closed_scrapped_id, "DF-MIG-0008"),
    ]:
        row = _case(case_id)
        before = before_rows[case_id]
        assert row["status"] == before["status"]
        assert row["disposition"] == before["disposition"]
        assert row["updated_at"] == before["updated_at"], "closed case must not be touched at all"
        assert _history_notes(case_id) == []
        assert _audit_rows(case_number) == []

    conn.close()


def test_phase7_migration_is_idempotent_on_a_second_run(tmp_path):
    """Re-running `alembic upgrade head` (e.g. a redeploy where the migration
    already applied) must not re-migrate or double-log anything."""
    db_path = tmp_path / "phase7_migration_idempotent.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"

    _run_alembic(["upgrade", PRE_MIGRATION_REVISION], env)

    conn = sqlite3.connect(str(db_path))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    station_id = conn.execute(
        "INSERT INTO stations (name, active, sort_order, created_at, updated_at) "
        "VALUES ('Test Station', 1, 0, ?, ?)",
        (now, now),
    ).lastrowid
    case_id = _seed_case(
        conn,
        case_number="DF-MIG-IDEMPOTENT",
        status="In Rework",
        disposition="Rework",
        station_id=station_id,
    )
    conn.commit()
    conn.close()

    _run_alembic(["upgrade", "head"], env)
    # Alembic itself refuses to re-run an already-applied migration on a normal
    # `upgrade head` (it tracks the applied revision in alembic_version), but
    # confirm explicitly that a second invocation is a true no-op regardless.
    _run_alembic(["upgrade", "head"], env)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM defect_cases WHERE id = ?", (case_id,)).fetchone()
    assert row["status"] == "Open"
    history = conn.execute(
        "SELECT * FROM status_history WHERE defect_case_id = ?", (case_id,)
    ).fetchall()
    assert len(history) == 1, "must not double-write a history row on a second upgrade run"
    conn.close()
