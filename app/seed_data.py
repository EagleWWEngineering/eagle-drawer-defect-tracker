"""Baseline master data (stations, categories, priorities, statuses, dispositions).

This module is the single source of truth for seed order/content, used by both the
Alembic data-seed step and by app startup (idempotent — safe to call every start).
Keep it in sync with docs/PROJECT_SPEC.md section 3.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import DefectCategory, Station

STATIONS: list[str] = [
    "Ripping & Picking",
    "Cross Cut",
    "Dovetail Machine",
    "Dado",
    "Assembly",
    "Putty",
    "Side Sanding",
    "Top Sanding",
    "Seal Coat",
    "Dry Time 1",
    "Prep Sanding",
    "Top Coat",
    "Dry Time 2",
    "Notch & Bore",
    "QC / Sorting / Shipping",
]

DEFECT_CATEGORIES: list[str] = [
    "Bad Wood / Material",
    "Cutting / Incorrect Dimension",
    "Dovetail / Machining",
    "Dado / Bottom Groove",
    "Bottom Panel",
    "Assembly / Joint / Glue / Staple",
    "Putty / Surface Fill",
    "Sanding / Surface",
    "Finish / Coating",
    "Notch & Bore",
    "Scoop / Custom Cutout",
    "Damage / Handling",
    "Wrong Feature / Orientation",
    "Other",
]

# Priorities, statuses, and dispositions are small fixed vocabularies enforced in
# app/services/defect_service.py rather than separate tables, since the pilot must
# not be able to invent new statuses without a corresponding transition-map update.
PRIORITIES: list[dict[str, str]] = [
    {"name": "Urgent", "color": "#c0392b"},
    {"name": "High", "color": "#d97706"},
    {"name": "Normal", "color": "#2563eb"},
]

STATUSES: list[str] = [
    "Open",
    "In Rework",
    "Waiting",
    "Ready for QC Recheck",
    "Closed - Repaired",
    "Closed - Scrapped",
    "Closed - Use As Is",
]

DISPOSITIONS: list[str] = ["Rework", "Scrap", "Use As Is", "Hold"]


def seed_master_data(db: Session) -> None:
    """Insert baseline stations/categories if they don't already exist (idempotent)."""
    existing_stations = {s.name for s in db.query(Station).all()}
    for order, name in enumerate(STATIONS, start=1):
        if name not in existing_stations:
            db.add(Station(name=name, active=True, sort_order=order))

    existing_categories = {c.name for c in db.query(DefectCategory).all()}
    for order, name in enumerate(DEFECT_CATEGORIES, start=1):
        if name not in existing_categories:
            db.add(DefectCategory(name=name, active=True, sort_order=order))

    db.commit()
