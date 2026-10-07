"""PROJECT_SPEC_PHASE10.md Part 2: label payload parsing (the one server-side
implementation), the order_lines/drawer-identity migration, and the front-end
wiring that hands raw QR text to the server instead of parsing it in JS."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.label_service import parse_label_text

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LABEL_SCAN_JS = (PROJECT_ROOT / "app" / "static" / "js" / "label-scan.js").read_text(
    encoding="utf-8"
)
DEFECT_ENTRY_HTML = (PROJECT_ROOT / "app" / "templates" / "defect_entry.html").read_text(
    encoding="utf-8"
)
BASE = "https://eagledovetaildrawers.sharepoint.com/:b:/r/sites/Server/Documents/AccessDB/"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Unique-ID label, single malformed backslash (as printed today).
        (
            BASE + "WorkOrderPDFs/\\179459.pdf#drawer=285016-1",
            {"order_no": "179459", "order_detail_id": 285016, "unit": 1},
        ),
        # Double backslash, and a multi-digit unit.
        (
            BASE + "WorkOrderPDFs/\\\\179459.pdf#drawer=285016-12",
            {"order_no": "179459", "order_detail_id": 285016, "unit": 12},
        ),
        # A normal forward slash.
        (
            BASE + "WorkOrderPDFs/179459.pdf#drawer=285016-1",
            {"order_no": "179459", "order_detail_id": 285016, "unit": 1},
        ),
        # URL-encoded fragment.
        (
            BASE + "WorkOrderPDFs/\\179459.pdf%23drawer%3D285016-2",
            {"order_no": "179459", "order_detail_id": 285016, "unit": 2},
        ),
        # Old order-only label.
        (
            BASE + "WorkOrderPDFs/\\178414.pdf",
            {"order_no": "178414", "order_detail_id": None, "unit": None},
        ),
        # Malformed fragment - order still read, no drawer identity.
        (
            BASE + "WorkOrderPDFs/\\179459.pdf#drawer=abc",
            {"order_no": "179459", "order_detail_id": None, "unit": None},
        ),
        # Not a work order label; a bare fragment is never trusted on its own.
        ("#drawer=285016-1", {"order_no": None, "order_detail_id": None, "unit": None}),
        ("", {"order_no": None, "order_detail_id": None, "unit": None}),
        (None, {"order_no": None, "order_detail_id": None, "unit": None}),
    ],
)
def test_parse_label_text(text, expected):
    assert parse_label_text(text) == expected


def test_label_scan_js_no_longer_parses_the_payload_itself():
    assert "\\d{6}" not in LABEL_SCAN_JS
    assert "extractOrderNumberFromQrText" not in LABEL_SCAN_JS
    assert "cb.onQrText" in LABEL_SCAN_JS


def test_defect_entry_resolves_labels_server_side_and_sends_drawer_identity():
    assert "/api/v1/labels/resolve" in DEFECT_ENTRY_HTML
    assert "onQrText" in DEFECT_ENTRY_HTML
    assert "currentDrawerIdentity()" in DEFECT_ENTRY_HTML
    assert 'id="scanned-drawer"' in DEFECT_ENTRY_HTML
    # 2026-10 redesign: no letter picker and no typed line - the line comes only
    # from the label; an order-only label or a typed work order saves no line.
    assert 'id="line-label-picker"' not in DEFECT_ENTRY_HTML
    assert 'type="hidden" id="line_label"' in DEFECT_ENTRY_HTML
    assert "possible_source_station_id" not in DEFECT_ENTRY_HTML


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


def test_migration_adds_order_lines_and_nullable_drawer_identity(tmp_path):
    db_path = tmp_path / "phase10_labels_migration.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"

    _run_alembic(["upgrade", "a7d2e9c4b1f0"], env)
    conn = sqlite3.connect(str(db_path))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    station_id = conn.execute(
        "INSERT INTO stations (name, active, sort_order, is_favorite, created_at, updated_at) "
        "VALUES ('S', 1, 0, 0, ?, ?)",
        (now, now),
    ).lastrowid
    conn.execute(
        "INSERT INTO defect_cases (case_number, production_date, detected_at, "
        "work_order_number, found_station_id, priority, status, resolved_on_the_spot, "
        "skipped_recheck, created_at, updated_at, is_deleted) "
        "VALUES ('DF-PRE-1', '2026-09-01', ?, '178414', ?, 'Normal', 'Open', 0, 0, ?, ?, 0)",
        (now, station_id, now, now),
    )
    conn.commit()
    conn.close()

    _run_alembic(["upgrade", "b8e3f0d5c2a1"], env)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT order_detail_id, drawer_unit FROM defect_cases").fetchone()
    assert (row["order_detail_id"], row["drawer_unit"]) == (None, None)
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(order_lines)")}
    assert set(cols) == {"order_detail_id", "order_no", "line", "qty", "detail_json", "received_at"}
    assert cols["order_detail_id"]["pk"] == 1
    conn.close()

    _run_alembic(["downgrade", "a7d2e9c4b1f0"], env)
    conn = sqlite3.connect(str(db_path))
    case_cols = {r[1] for r in conn.execute("PRAGMA table_info(defect_cases)")}
    assert "order_detail_id" not in case_cols
    assert conn.execute("SELECT count(*) FROM defect_cases").fetchone()[0] == 1
    conn.close()


def test_new_defect_can_read_the_label_from_a_photo():
    """2026-10-07: the live camera view needs https; on eagle-vm (plain http) the
    scan button takes a photo with the camera app and reads the QR from it."""
    assert 'id="label-photo-input"' in DEFECT_ENTRY_HTML
    assert 'capture="environment"' in DEFECT_ENTRY_HTML
    assert "LabelScan.liveCameraAvailable()" in DEFECT_ENTRY_HTML
    assert "LabelScan.decodeImageFile(file)" in DEFECT_ENTRY_HTML
    assert "async function decodeImageFile" in LABEL_SCAN_JS
    assert "window.isSecureContext" in LABEL_SCAN_JS


def test_new_defect_uses_the_https_scanner_page_when_the_live_camera_is_blocked():
    """2026-10-07: Safari only allows the live camera on https; on eagle-vm (http)
    the Scan button opens the https scanner page (GitHub Pages), which returns
    with #scan=<code>."""
    assert "label_scanner_url | tojson" in DEFECT_ENTRY_HTML
    assert '"?return=" + encodeURIComponent(back)' in DEFECT_ENTRY_HTML
    assert 'window.location.hash.startsWith("#scan=")' in DEFECT_ENTRY_HTML
    assert 'id="wo-photo-btn"' in DEFECT_ENTRY_HTML
