"""Baseline master data (stations, categories, priorities, statuses, dispositions).

This module is the single source of truth for seed order/content, used by both the
Alembic data-seed step and by app startup (idempotent — safe to call every start).
Keep it in sync with docs/PROJECT_SPEC.md section 3.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppSetting, CustomerIssueCategory, DefectCategory, Station
from app.services.auth_service import sync_credentials_from_env

# Phase 4: cost tracking settings (app_settings key-value table).
COST_PER_DRAWER_SETTING_KEY = "cost_per_drawer"

STATIONS: list[str] = [
    "Ripping & Picking",
    "Upcut",
    "Dovetail Machine",
    "Dado",
    "Assembly",
    "Bottom Panel",
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

# Stations seeded under an old name that's since been corrected. Renamed IN PLACE
# (never delete+recreate) so the Station keeps its id - any existing DefectCase/
# CustomerIssue row referencing it by station_id is unaffected by the rename.
STATION_RENAMES: dict[str, str] = {"Cross Cut": "Upcut"}

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

# Phase 2: customer-reported issue categories (kept separate from internal
# DEFECT_CATEGORIES - see PROJECT_SPEC_PHASE2.md).
CUSTOMER_ISSUE_CATEGORIES: list[str] = [
    "Wrong Size",
    "Wrong Spec",
    "Joinery",
    "Finish Quality",
    "Missing Parts",
    "Shipping Damage / Crushed Box",
    "Corner Impact",
    "Warp or Crack",
    "Hinge Holes",
    "Other",
]

CUSTOMER_ISSUE_SOURCE_TYPES: list[str] = ["Manufacturing", "Shipping Damage"]
CUSTOMER_ISSUE_STATUSES: list[str] = ["Open", "Ignored", "Linked"]


def _apply_station_renames(db: Session) -> None:
    """Rename a station in place wherever an old seeded name is still in the
    database (idempotent - a no-op once the rename has happened once)."""
    for old_name, new_name in STATION_RENAMES.items():
        old = db.query(Station).filter(Station.name == old_name).first()
        if old is None:
            continue
        if db.query(Station).filter(Station.name == new_name).first() is not None:
            continue  # new name already exists (e.g. added by hand) - don't clobber it
        old.name = new_name


def seed_master_data(db: Session) -> None:
    """Insert baseline stations/categories if they don't already exist (idempotent)."""
    _apply_station_renames(db)

    existing_stations = {s.name for s in db.query(Station).all()}
    for order, name in enumerate(STATIONS, start=1):
        if name not in existing_stations:
            db.add(Station(name=name, active=True, sort_order=order))

    existing_categories = {c.name for c in db.query(DefectCategory).all()}
    for order, name in enumerate(DEFECT_CATEGORIES, start=1):
        if name not in existing_categories:
            db.add(DefectCategory(name=name, active=True, sort_order=order))

    existing_customer_categories = {c.name for c in db.query(CustomerIssueCategory).all()}
    for order, name in enumerate(CUSTOMER_ISSUE_CATEGORIES, start=1):
        if name not in existing_customer_categories:
            db.add(CustomerIssueCategory(name=name, active=True, sort_order=order))

    if db.get(AppSetting, COST_PER_DRAWER_SETTING_KEY) is None:
        db.add(
            AppSetting(
                key=COST_PER_DRAWER_SETTING_KEY,
                value=str(get_settings().default_cost_per_drawer),
            )
        )

    # Phase 5 incident fix: re-sync the shared-login credential from the
    # environment on every startup, not just once against an empty database -
    # see app/services/auth_service.py sync_credentials_from_env().
    sync_credentials_from_env(db)

    db.commit()
