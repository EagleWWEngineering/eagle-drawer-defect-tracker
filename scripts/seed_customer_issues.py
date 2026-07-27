#!/usr/bin/env python
"""Populate the database with synthetic customer-reported issues (Phase 2 MVP).

All customer names here are made up placeholders - not real Eagle Woodworking
customers from any source. Safe to run against a throwaway/demo database; do NOT
run it against a database with real customer data.

Usage:
    python scripts/seed_customer_issues.py [--count 18] [--days 14] [--seed 7]

Uses app/services/customer_issue_service.py, so seeded issues obey the same
validation and auto-cost-calculation rules real entries would (once this MVP is
wired to the real daily production brief source).
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
from app.models import CustomerIssueCategory, DefectCase  # noqa: E402
from app.seed_data import seed_master_data  # noqa: E402
from app.services import customer_issue_service  # noqa: E402

FAKE_CUSTOMER_NAMES = [
    "Jordan Ellis",
    "Morgan Lee",
    "Taylor Brooks",
    "Casey Nguyen",
    "Riley Thompson",
    "Avery Martinez",
    "Sam Patterson",
    "Jamie Whitfield",
    "Drew Sullivan",
    "Quinn Alvarez",
    "Reese Donovan",
    "Cameron Blake",
]

SHOULD_HAVE_CAUGHT_OPTIONS = ["QA/Final", "Assembly", "Finish"]

DESCRIPTIONS_BY_CATEGORY = {
    "Wrong Size": "Drawer box does not match ordered dimensions - customer measured it.",
    "Wrong Spec": "Wrong wood species / finish spec shipped versus what was ordered.",
    "Joinery": "Dovetail joints are loose / gapping on delivery.",
    "Finish Quality": "Customer reports blotchy or uneven finish coat on visible surfaces.",
    "Missing Parts": "Drawer arrived without slides / hardware that should have been included.",
    "Shipping Damage / Crushed Box": "Outer packaging arrived crushed; product has visible dents.",
    "Corner Impact": "One corner of the drawer box is cracked/chipped, consistent with transit.",
    "Warp or Crack": "Side panel has warped or cracked after delivery.",
    "Hinge Holes": "Hinge boring is misaligned or missing entirely.",
    "Other": "Customer-reported issue that doesn't fit the standard categories.",
}


def _random_order_number(rng: random.Random) -> str | None:
    if rng.random() < 0.3:
        return None  # "order not identified"
    prefix = rng.choice(["SO", "PO"])
    return f"{prefix}-{rng.randint(7000, 9999)}"


def _weighted_category(
    rng: random.Random, categories: list[CustomerIssueCategory]
) -> CustomerIssueCategory:
    weighted_names = [
        "Wrong Size",
        "Wrong Size",
        "Wrong Spec",
        "Wrong Spec",
        "Finish Quality",
        "Finish Quality",
    ]
    by_name = {c.name: c for c in categories}
    weighted_names = [n for n in weighted_names if n in by_name]
    if rng.random() < 0.65 and weighted_names:
        return by_name[rng.choice(weighted_names)]
    return rng.choice(categories)


def seed_customer_issues(count: int, days: int, seed: int) -> None:
    rng = random.Random(seed)
    db = SessionLocal()
    try:
        seed_master_data(db)
        categories = (
            db.query(CustomerIssueCategory).filter(CustomerIssueCategory.active.is_(True)).all()
        )
        existing_cases = (
            db.query(DefectCase).filter(DefectCase.is_deleted.is_(False)).limit(20).all()
        )

        created_issues = []
        for _ in range(count):
            day_offset = rng.randint(0, days - 1)
            reported_date = dt.date.today() - dt.timedelta(days=day_offset)
            category = _weighted_category(rng, categories)
            source_type = "Manufacturing" if rng.random() < 0.8 else "Shipping Damage"
            should_have_caught_at = rng.choice([*SHOULD_HAVE_CAUGHT_OPTIONS, None])
            piece_count = rng.randint(1, 9)

            issue = customer_issue_service.create_customer_issue(
                db,
                reported_date=reported_date,
                customer_name=rng.choice(FAKE_CUSTOMER_NAMES),
                order_number=_random_order_number(rng),
                issue_category_id=category.id,
                source_type=source_type,
                should_have_caught_at=should_have_caught_at,
                piece_count=piece_count,
                estimated_rework_cost=None,  # let the service auto-calculate piece_count * $100
                description=DESCRIPTIONS_BY_CATEGORY.get(
                    category.name, "Customer-reported quality issue."
                ),
                photo_urls=None,
                notes=None,
            )
            created_issues.append(issue)

        # Link a couple of issues to existing internal defect cases, if any exist,
        # to demonstrate the link workflow in the UI.
        linked_count = 0
        if existing_cases:
            for issue in rng.sample(created_issues, k=min(2, len(created_issues))):
                case = rng.choice(existing_cases)
                customer_issue_service.link_to_defect_case(db, issue, case.id)
                linked_count += 1

        print(
            f"Seeded {len(created_issues)} synthetic customer issues across the last {days} days."
        )
        print(f"Linked {linked_count} of them to existing internal defect cases.")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--count", type=int, default=18, help="How many customer issues to generate (15-20 typical)"
    )
    parser.add_argument(
        "--days", type=int, default=14, help="Spread issues across this many past calendar days"
    )
    parser.add_argument(
        "--seed", type=int, default=7, help="Random seed for reproducible demo data"
    )
    args = parser.parse_args()
    seed_customer_issues(count=args.count, days=args.days, seed=args.seed)


if __name__ == "__main__":
    main()
