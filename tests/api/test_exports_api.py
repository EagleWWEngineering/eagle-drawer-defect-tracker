"""CSV export must respect active filters and include raw counts/identifiers."""

from __future__ import annotations

import csv
import io


def _create_case(client, master_data, category_name, wo, **overrides):
    payload = {
        "production_date": "2026-07-24",
        "detected_at": "2026-07-24T14:30:00Z",
        "work_order_number": wo,
        "found_station_id": master_data["stations"]["QC / Sorting / Shipping"],
        "priority": "Normal",
        "items": [
            {
                "defect_category_id": master_data["categories"][category_name],
                "affected_drawer_quantity": 1,
            }
        ],
    }
    payload.update(overrides)
    return client.post("/api/v1/defect-cases", json=payload).json()


def test_csv_export_matches_filtered_selection(client, master_data):
    _create_case(client, master_data, "Sanding / Surface", "WO-CSV-A")
    _create_case(client, master_data, "Dado / Bottom Groove", "WO-CSV-B")

    resp = client.get("/api/v1/exports/defects.csv", params={"work_order_number": "WO-CSV-A"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")

    rows = list(csv.reader(io.StringIO(resp.text)))
    header, data_rows = rows[0], rows[1:]
    assert "case_number" in header
    assert "affected_drawer_quantity" in header
    assert len(data_rows) == 1
    assert data_rows[0][header.index("work_order_number")] == "WO-CSV-A"


def test_csv_export_all_rows_when_unfiltered(client, master_data):
    _create_case(client, master_data, "Sanding / Surface", "WO-CSV-C")
    _create_case(client, master_data, "Dado / Bottom Groove", "WO-CSV-D")

    resp = client.get("/api/v1/exports/defects.csv")
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert len(rows) - 1 == 2  # header + 2 data rows


def test_csv_export_includes_case_cost_columns(client, master_data):
    """PROJECT_SPEC_PHASE7.md "Cost model": cost is per-CASE now, not joined by
    production_date from a Daily Production Summary row - so these columns are
    populated from the case's own snapshot, independent of whether a summary
    exists for that date at all."""
    _create_case(client, master_data, "Sanding / Surface", "WO-CSV-COST")

    resp = client.get("/api/v1/exports/defects.csv", params={"work_order_number": "WO-CSV-COST"})
    rows = list(csv.reader(io.StringIO(resp.text)))
    header, data_rows = rows[0], rows[1:]
    assert "case_cost_per_drawer" in header
    assert "case_internal_cost" in header
    assert "case_cost_avoided" in header
    # Scrap cost was dropped from this app entirely (docs/PROJECT_SPEC_PHASE4.md
    # "Scrap removal") - no scrap column in the CSV export. The old date-joined
    # columns are gone too.
    assert "day_cost_per_drawer" not in header
    assert "day_internal_rework_cost" not in header
    assert "day_internal_scrap_cost" not in header
    row = data_rows[0]
    assert row[header.index("case_cost_per_drawer")] == "35.0"
    assert row[header.index("case_internal_cost")] == "35.0"
    assert row[header.index("case_cost_avoided")] == "0.0"


def test_csv_export_use_as_is_case_cost_is_zero_and_avoided_is_populated(client, master_data):
    _create_case(
        client,
        master_data,
        "Sanding / Surface",
        "WO-CSV-USEASIS",
        disposition="Rework",
        resolved_on_the_spot=True,
        instant_close_outcome="Use As Is",
        repair_action="Buyer accepted as-is",
    )
    resp = client.get("/api/v1/exports/defects.csv", params={"work_order_number": "WO-CSV-USEASIS"})
    rows = list(csv.reader(io.StringIO(resp.text)))
    header, data_rows = rows[0], rows[1:]
    row = data_rows[0]
    assert row[header.index("case_internal_cost")] == "0.0"
    assert row[header.index("case_cost_avoided")] == "35.0"


def test_csv_export_has_its_own_line_column(client, master_data):
    _create_case(client, master_data, "Sanding / Surface", "178414", line_label="E")
    _create_case(client, master_data, "Sanding / Surface", "178414")  # no line

    resp = client.get("/api/v1/exports/defects.csv", params={"work_order_number": "178414"})
    rows = list(csv.reader(io.StringIO(resp.text)))
    header, data_rows = rows[0], rows[1:]
    assert "line_label" in header
    line_idx = header.index("line_label")
    values = {row[line_idx] for row in data_rows}
    assert values == {"E", ""}
    # Not concatenated into work_order_number - that column stays the raw number.
    wo_idx = header.index("work_order_number")
    assert all(row[wo_idx] == "178414" for row in data_rows)


def test_csv_export_filters_by_line_label(client, master_data):
    _create_case(client, master_data, "Sanding / Surface", "178414", line_label="E")
    _create_case(client, master_data, "Dado / Bottom Groove", "178414", line_label="F")

    resp = client.get(
        "/api/v1/exports/defects.csv",
        params={"work_order_number": "178414", "line_label": "E"},
    )
    rows = list(csv.reader(io.StringIO(resp.text)))
    header, data_rows = rows[0], rows[1:]
    assert len(data_rows) == 1
    assert data_rows[0][header.index("line_label")] == "E"


def test_export_is_audited(client, master_data):
    _create_case(client, master_data, "Sanding / Surface", "WO-CSV-E")
    client.get("/api/v1/exports/defects.csv")

    from app.models import AuditLog

    session = client.testing_sessionmaker()
    entries = session.query(AuditLog).filter(AuditLog.action == "export").all()
    assert len(entries) == 1
    session.close()
