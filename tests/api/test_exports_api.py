"""CSV export must respect active filters and include raw counts/identifiers."""

from __future__ import annotations

import csv
import io


def _create_case(client, master_data, category_name, wo):
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


def test_export_is_audited(client, master_data):
    _create_case(client, master_data, "Sanding / Surface", "WO-CSV-E")
    client.get("/api/v1/exports/defects.csv")

    from app.models import AuditLog

    session = client.testing_sessionmaker()
    entries = session.query(AuditLog).filter(AuditLog.action == "export").all()
    assert len(entries) == 1
    session.close()
