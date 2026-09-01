"""Alembic migration test for PROJECT_SPEC_PHASE9.md Part 1: two new nullable
columns on defect_cases (line_label, entry_source) plus an index on
(work_order_number, line_label). Purely additive - no data transform, no drop,
no backfill - so this only needs to confirm the schema change applies cleanly
and that a pre-existing row (seeded before this migration ran) reads back with
NULL for both new columns, never some default/backfilled value.

Runs the real migrations via `python -m alembic` in a subprocess against a
throwaway SQLite file, same approach as tests/unit/test_phase7_migration.py -
alembic/env.py re-reads DATABASE_URL from the environment at import time.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# The revision right before this phase's migration - schema-complete otherwise.
PRE_MIGRATION_REVISION = "7c1f9a2b4e6d"
HEAD_REVISION = "8f1c2d3a4b5e"


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


def test_migration_adds_nullable_line_label_and_entry_source(tmp_path):
    db_path = tmp_path / "phase9_migration_test.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"

    # 1. Build the schema up to (but not including) this migration, and seed one
    #    pre-existing case the old way - no line_label/entry_source columns exist
    #    yet at this point.
    _run_alembic(["upgrade", PRE_MIGRATION_REVISION], env)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    station_id = conn.execute(
        "INSERT INTO stations (name, active, sort_order, created_at, updated_at) "
        "VALUES ('Test Station', 1, 0, ?, ?)",
        (now, now),
    ).lastrowid
    case_id = conn.execute(
        """
        INSERT INTO defect_cases (
            case_number, production_date, detected_at, work_order_number,
            found_station_id, priority, status, disposition,
            resolved_on_the_spot, skipped_recheck, created_at, updated_at, is_deleted
        ) VALUES (
            'DF-PRE-0001', '2026-08-01', ?, '178414', ?, 'Normal', 'Open', NULL, 0, 0, ?, ?, 0
        )
        """,
        (now, station_id, now, now),
    ).lastrowid
    conn.commit()
    conn.close()

    # 2. Run this phase's migration.
    _run_alembic(["upgrade", HEAD_REVISION], env)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(defect_cases)").fetchall()}
    assert "line_label" in columns
    assert "entry_source" in columns

    # 3. The pre-existing row was never touched/backfilled - both new columns
    #    read back as NULL, not '' or some computed default.
    row = conn.execute("SELECT * FROM defect_cases WHERE id = ?", (case_id,)).fetchone()
    assert row["line_label"] is None
    assert row["entry_source"] is None
    # Nothing else about the row changed.
    assert row["work_order_number"] == "178414"
    assert row["status"] == "Open"

    # 4. The composite index exists.
    indexes = {row["name"] for row in conn.execute("PRAGMA index_list(defect_cases)").fetchall()}
    assert "ix_defect_cases_work_order_line" in indexes

    conn.close()


def test_migration_upgrade_is_idempotent_on_a_second_run(tmp_path):
    db_path = tmp_path / "phase9_migration_idempotent.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"

    _run_alembic(["upgrade", "head"], env)
    # A second `upgrade head` against an already-migrated database must not error.
    _run_alembic(["upgrade", "head"], env)

    conn = sqlite3.connect(str(db_path))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(defect_cases)").fetchall()}
    assert {"line_label", "entry_source"} <= columns
    conn.close()


def test_migration_two_cases_may_share_the_same_order_and_line(tmp_path):
    """No unique constraint on (work_order_number, line_label) - the pair must be
    freely repeatable across many cases (PROJECT_SPEC_PHASE9.md Part 1)."""
    db_path = tmp_path / "phase9_migration_dupes.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"

    _run_alembic(["upgrade", "head"], env)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    station_id = conn.execute(
        "INSERT INTO stations (name, active, sort_order, created_at, updated_at) "
        "VALUES ('Test Station', 1, 0, ?, ?)",
        (now, now),
    ).lastrowid
    for case_number in ("DF-DUPE-0001", "DF-DUPE-0002"):
        conn.execute(
            """
            INSERT INTO defect_cases (
                case_number, production_date, detected_at, work_order_number,
                line_label, found_station_id, priority, status, disposition,
                resolved_on_the_spot, skipped_recheck, created_at, updated_at, is_deleted
            ) VALUES (?, '2026-08-01', ?, '178414', 'E', ?, 'Normal', 'Open', NULL, 0, 0, ?, ?, 0)
            """,
            (case_number, now, station_id, now, now),
        )
    conn.commit()

    count = conn.execute(
        "SELECT COUNT(*) FROM defect_cases WHERE work_order_number = '178414' AND line_label = 'E'"
    ).fetchone()[0]
    assert count == 2
    conn.close()
