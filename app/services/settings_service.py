"""Generic app_settings key-value store (Phase 4). Currently just cost_per_drawer,
but reads/writes go through here rather than directly against the model so a
future setting doesn't need bespoke plumbing.

Historical note: DailyProductionSummary.cost_per_drawer_at_time snapshots
whatever get_cost_per_drawer() returns at save time (see
app/services/defect_service.py upsert_daily_summary) - changing the rate here
only affects future saves, never rewrites past summaries.
"""

from __future__ import annotations

import decimal

from sqlalchemy.orm import Session

from app.errors import ValidationError
from app.models import AppSetting
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
