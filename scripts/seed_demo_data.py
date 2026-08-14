#!/usr/bin/env python
"""Populate the database with synthetic demo data for exploring the dashboard,
reports, and rework queue without waiting for real shop-floor entries.

All data here is made up (fake work order numbers, random counts) - there is no
real customer or production information in this script. Safe to run against a
throwaway/demo database; do NOT run it against a database with real shop data,
since it will add fictitious cases alongside the real ones.

Usage:
    python scripts/seed_demo_data.py [--days 21] [--seed 42]

Uses the same service-layer functions (app/services/defect_service.py) the REST API
uses, so the synthetic data obeys the same counting and validation rules as real
data entered through the UI.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SessionLocal  # noqa: E402
from app.models import DefectCategory, Station  # noqa: E402
from app.seed_data import seed_master_data  # noqa: E402
from app.services import defect_service  # noqa: E402

WORK_ORDER_PREFIXES = ["WO-DEMO"]


def _random_detected_at(base_date: dt.date) -> dt.datetime:
    hour = random.randint(6, 15)
    minute = random.randint(0, 59)
    return dt.datetime(
        base_date.year, base_date.month, base_date.day, hour, minute, tzinfo=dt.timezone.utc
    )


def seed_demo_data(days: int, seed: int) -> None:
    random.seed(seed)
    db = SessionLocal()
    try:
        seed_master_data(db)
        stations = db.query(Station).filter(Station.active.is_(True)).all()
        categories = db.query(DefectCategory).filter(DefectCategory.active.is_(True)).all()
        qc_station = next(s for s in stations if s.name == "QC / Sorting / Shipping")

        today = dt.date.today()
        case_counter = 0

        for day_offset in range(days, 0, -1):
            production_date = today - dt.timedelta(days=day_offset)
            if production_date.weekday() >= 5:  # skip weekends, like a real shop
                continue

            inspected = random.randint(60, 140)
            rejected = random.randint(0, max(1, inspected // 12))
            reworked = random.randint(0, rejected)
            scrapped = max(0, min(rejected - reworked, random.randint(0, 2)))

            defect_service.upsert_daily_summary(
                db,
                production_date=production_date,
                shift="Day",
                drawers_inspected=inspected,
                drawers_rejected_unique=rejected,
                drawers_reworked=reworked,
                drawers_scrapped=scrapped,
                notes=None,
            )

            num_cases_today = random.randint(0, 4)
            for _ in range(num_cases_today):
                case_counter += 1
                found_station = random.choice(stations)
                possible_source = random.choice([None, *stations])
                priority = random.choices(["Normal", "High", "Urgent"], weights=[0.6, 0.3, 0.1])[0]
                num_categories = random.choices([1, 2], weights=[0.75, 0.25])[0]
                chosen_categories = random.sample(
                    categories, k=min(num_categories, len(categories))
                )

                items = [
                    {
                        "defect_category_id": c.id,
                        "affected_drawer_quantity": random.choices(
                            [1, 2, 3], weights=[0.7, 0.2, 0.1]
                        )[0],
                    }
                    for c in chosen_categories
                ]

                case = defect_service.create_defect_case(
                    db,
                    production_date=production_date,
                    detected_at=_random_detected_at(production_date),
                    work_order_number=f"{random.choice(WORK_ORDER_PREFIXES)}-{1000 + case_counter}",
                    drawer_part_reference=None,
                    found_station_id=found_station.id,
                    possible_source_station_id=(possible_source.id if possible_source else None),
                    priority=priority,
                    items=items,
                )

                # Age most cases forward through a plausible status, so the Rework
                # Queue and Reports pages both have interesting non-Open data. Scrap
                # is deliberately rare (PROJECT_SPEC.md section 3.2 - only the last
                # 5%) to match the shop floor, where an operator almost always
                # reworks the part in hand rather than scrapping it.
                roll = random.random()
                if roll < 0.30:
                    pass  # leave as Open
                elif roll < 0.50:
                    defect_service.update_case_status(db, case, new_status="In Rework")
                elif roll < 0.60:
                    defect_service.update_case_status(db, case, new_status="Waiting")
                elif roll < 0.95:
                    defect_service.update_case_status(db, case, new_status="In Rework")
                    defect_service.update_case_status(db, case, new_status="Ready for QC Recheck")
                    defect_service.update_case_status(
                        db,
                        case,
                        new_status="Closed - Repaired",
                        disposition="Rework",
                        repair_action="Re-sanded and re-finished the affected surface.",
                        note="Re-sanded and re-finished the affected surface.",
                    )
                else:
                    # Rare case (PROJECT_SPEC.md section 3.2: Scrap is genuinely
                    # uncommon on the shop floor). The note is optional for a normal
                    # close, but demo data reads better with one on record anyway.
                    defect_service.update_case_status(
                        db,
                        case,
                        new_status="Closed - Scrapped",
                        disposition="Scrap",
                        note="Confirmed scrapped - not repairable.",
                    )

        print(f"Seeded {case_counter} synthetic defect cases across {days} calendar days.")
        print(f"Found station used as default QC point: {qc_station.name}")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=21, help="How many past calendar days to generate"
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="Random seed for reproducible demo data"
    )
    args = parser.parse_args()
    seed_demo_data(days=args.days, seed=args.seed)


if __name__ == "__main__":
    main()
