"""PROJECT_SPEC_PHASE10.md Part 1: completed (fed) and rejected (hand-entered)
are independent KPIs - rates must survive rejected > inspected and
inspected == 0, and the ck_rejected_le_inspected drop migration must keep every
other constraint on daily_production_summaries.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.services import metrics_service
from app.services.defect_service import create_defect_case, upsert_daily_summary

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PRE_MIGRATION_REVISION = "4e9b5dea94ec"
MIGRATION_REVISION = "a7d2e9c4b1f0"


def test_kpis_with_rejected_greater_than_inspected_do_not_go_negative():
    kpis = metrics_service.compute_kpis(
        drawers_inspected=5, defect_events=8, unique_drawers_rejected=7, drawers_reworked=6
    ).to_dict()
    assert kpis["rejection_rate"] == 140.0
    assert kpis["first_pass_yield"] == 0.0  # floored, never negative
    assert kpis["defects_per_100"] == 160.0
    assert kpis["rework_rate"] == 120.0


def test_kpis_with_zero_inspected_and_nonzero_rejected_are_null_not_errors():
    kpis = metrics_service.compute_kpis(
        drawers_inspected=0, defect_events=3, unique_drawers_rejected=3, drawers_reworked=1
    ).to_dict()
    for key in ("defects_per_100", "rejection_rate", "first_pass_yield", "rework_rate"):
        assert kpis[key] is None
    assert kpis["quality_cost_per_drawer_inspected"] is None


def test_first_pass_yield_unchanged_for_normal_days():
    kpis = metrics_service.compute_kpis(
        drawers_inspected=100, defect_events=12, unique_drawers_rejected=10, drawers_reworked=4
    ).to_dict()
    assert kpis["first_pass_yield"] == 90.0


def test_db_accepts_rejected_greater_than_inspected(db_session, today, feed_completed):
    feed_completed({today: 2})
    row, _ = upsert_daily_summary(
        db_session, production_date=today, shift="Day", drawers_rejected_unique=9, notes=None
    )
    assert (row.drawers_inspected, row.drawers_rejected_unique) == (2, 9)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _run_alembic(args: list[str], env: dict) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic {args} failed:\n{result.stdout}\n{result.stderr}"


def _insert(conn, date: str, inspected: int, rejected: int) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO daily_production_summaries (production_date, shift, drawers_inspected, "
        "drawers_rejected_unique, drawers_reworked, drawers_scrapped, created_at, updated_at) "
        "VALUES (?, 'Day', ?, ?, 0, 0, ?, ?)",
        (date, inspected, rejected, now, now),
    )


def test_migration_drops_only_ck_rejected_le_inspected(tmp_path):
    db_path = tmp_path / "phase10_migration_test.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"

    _run_alembic(["upgrade", PRE_MIGRATION_REVISION], env)
    conn = sqlite3.connect(str(db_path))
    _insert(conn, "2026-09-01", 100, 10)  # pre-existing hand-typed history
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "2026-09-02", 5, 6)
    conn.close()

    _run_alembic(["upgrade", MIGRATION_REVISION], env)
    conn = sqlite3.connect(str(db_path))
    _insert(conn, "2026-09-02", 5, 6)  # now allowed
    conn.commit()
    rows = conn.execute(
        "SELECT production_date, drawers_inspected, drawers_rejected_unique "
        "FROM daily_production_summaries ORDER BY production_date"
    ).fetchall()
    assert rows == [("2026-09-01", 100, 10), ("2026-09-02", 5, 6)]

    # Every other rule still holds.
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "2026-09-03", -1, 0)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "2026-09-03", 1, -1)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "2026-09-01", 1, 0)  # unique (production_date, shift)
    indexes = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='daily_production_summaries'"
        )
    }
    assert "ix_daily_production_summaries_production_date" in indexes
    conn.close()


def test_create_defect_case_unaffected(db_session, stations, categories, today):
    """Sanity: defect creation doesn't depend on a summary row existing."""
    case = create_defect_case(
        db_session,
        production_date=today,
        detected_at=dt.datetime(2026, 7, 24, 9, tzinfo=dt.timezone.utc),
        work_order_number="WO-X",
        drawer_part_reference=None,
        found_station_id=stations["QC / Sorting / Shipping"].id,
        possible_source_station_id=None,
        priority="Normal",
        items=[{"defect_category_id": categories["Sanding / Surface"].id}],
    )
    assert case.id
