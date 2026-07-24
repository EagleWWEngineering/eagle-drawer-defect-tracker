"""PROJECT_SPEC.md section 9 (Pareto ordering) and rework-queue priority sort order."""

from __future__ import annotations

from app.services.metrics_service import compute_pareto, priority_sort_index


def test_pareto_sorted_descending_with_cumulative_percentage():
    rows = compute_pareto({"Sanding / Surface": 5, "Dado / Bottom Groove": 3, "Other": 2})

    assert [r["label"] for r in rows] == ["Sanding / Surface", "Dado / Bottom Groove", "Other"]
    assert rows[0]["cumulative_pct"] == 50.0
    assert rows[1]["cumulative_pct"] == 80.0
    assert rows[2]["cumulative_pct"] == 100.0
    assert sum(r["defect_events"] for r in rows) == 10


def test_pareto_total_matches_filtered_record_total():
    counts = {"A": 4, "B": 6}
    rows = compute_pareto(counts)
    assert sum(r["defect_events"] for r in rows) == sum(counts.values())


def test_priority_sort_urgent_first_then_high_then_normal():
    priorities = ["Normal", "Urgent", "High", "Normal", "Urgent"]
    ordered = sorted(priorities, key=priority_sort_index)
    assert ordered == ["Urgent", "Urgent", "High", "Normal", "Normal"]


def test_rework_queue_sorts_priority_then_oldest_first(db_session, stations, categories, today):
    import datetime as dt

    from app.services.defect_service import create_defect_case

    def make(wo, priority, hour):
        return create_defect_case(
            db_session,
            production_date=today,
            detected_at=dt.datetime(2026, 7, 24, hour, 0, tzinfo=dt.timezone.utc),
            work_order_number=wo,
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

    c1 = make("WO-A", "Normal", 8)
    c2 = make("WO-B", "Urgent", 10)
    c3 = make("WO-C", "Urgent", 9)
    c4 = make("WO-D", "High", 7)

    cases = [c1, c2, c3, c4]
    ordered = sorted(cases, key=lambda c: (priority_sort_index(c.priority), c.detected_at))

    assert [c.work_order_number for c in ordered] == ["WO-C", "WO-B", "WO-D", "WO-A"]
