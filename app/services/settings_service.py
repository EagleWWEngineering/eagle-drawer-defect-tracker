"""Generic app_settings key-value store (Phase 4): cost_per_drawer, plus the
Phase 11 UNDO-card kickback categories. Reads/writes go through here rather
than directly against the model so a future setting doesn't need bespoke
plumbing.

Historical note: DailyProductionSummary.cost_per_drawer_at_time snapshots
whatever get_cost_per_drawer() returns at save time (see
app/services/defect_service.py upsert_daily_summary) - changing the rate here
only affects future saves, never rewrites past summaries.
"""

from __future__ import annotations

import decimal

from sqlalchemy.orm import Session

from app.errors import ValidationError
from app.models import AppSetting, DefectCategory
from app.seed_data import COST_PER_DRAWER_SETTING_KEY

DEFAULT_FALLBACK_COST_PER_DRAWER = decimal.Decimal("35.00")


def get_cost_per_drawer(db: Session) -> decimal.Decimal:
    """The currently active rate. Falls back to the documented default if the
    app_settings row is somehow missing (should not happen once seeded)."""
    setting = db.get(AppSetting, COST_PER_DRAWER_SETTING_KEY)
    if setting is None:
        return DEFAULT_FALLBACK_COST_PER_DRAWER
    return decimal.Decimal(setting.value)


def set_cost_per_drawer(db: Session, value: decimal.Decimal) -> decimal.Decimal:
    if value <= 0:
        raise ValidationError(
            "Average drawer production cost must be greater than zero.",
            field="cost_per_drawer",
        )
    setting = db.get(AppSetting, COST_PER_DRAWER_SETTING_KEY)
    if setting is None:
        setting = AppSetting(key=COST_PER_DRAWER_SETTING_KEY, value=str(value))
        db.add(setting)
    else:
        setting.value = str(value)
    db.commit()
    db.refresh(setting)
    return decimal.Decimal(setting.value)


# PROJECT_SPEC_PHASE11.md: which defect category an UNDO-card kickback case
# gets, per area ("qc" / "assembly"). Stores the category's id, so renaming it
# in Admin never breaks the link. Unset (or pointing at a category that no
# longer exists) -> the built-in Other category - see drawer_event_service.
UNDO_CATEGORY_SETTING_KEYS: dict[str, str] = {
    "qc": "undo_category_qc",
    "assembly": "undo_category_assembly",
}


def get_undo_category_id(db: Session, area: str) -> int | None:
    setting = db.get(AppSetting, UNDO_CATEGORY_SETTING_KEYS[area])
    if setting is None or not setting.value:
        return None
    return int(setting.value)


def set_undo_category_ids(db: Session, ids: dict[str, int | None]) -> dict[str, int | None]:
    """Save both areas at once. None clears an area back to the default."""
    for area, category_id in ids.items():
        if category_id is not None and db.get(DefectCategory, category_id) is None:
            raise ValidationError(
                f"Defect category {category_id} doesn't exist.", field=f"{area}_category_id"
            )
    for area, category_id in ids.items():
        key = UNDO_CATEGORY_SETTING_KEYS[area]
        value = str(category_id) if category_id is not None else ""
        setting = db.get(AppSetting, key)
        if setting is None:
            db.add(AppSetting(key=key, value=value))
        else:
            setting.value = value
    db.commit()
    return {area: get_undo_category_id(db, area) for area in UNDO_CATEGORY_SETTING_KEYS}


# 2026-10 redesign: the defects-per-100 target drawn on the Quality Today
# dashboard's trend. Blank = no target line. Set in Admin.
QUALITY_TARGET_KEY = "quality_target_per_100"


def get_quality_target(db: Session) -> float | None:
    setting = db.get(AppSetting, QUALITY_TARGET_KEY)
    if setting is None or not setting.value:
        return None
    return float(setting.value)


def set_quality_target(db: Session, value: float | None) -> float | None:
    if value is not None and not (0 < value < 1000):
        raise ValidationError("The target must be a number above 0.", field="target_per_100")
    setting = db.get(AppSetting, QUALITY_TARGET_KEY)
    stored = "" if value is None else f"{value:g}"
    if setting is None:
        db.add(AppSetting(key=QUALITY_TARGET_KEY, value=stored))
    else:
        setting.value = stored
    db.commit()
    return get_quality_target(db)
