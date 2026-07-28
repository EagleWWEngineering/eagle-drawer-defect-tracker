"""Unit tests for app/services/settings_service.py (Phase 4 cost tracking)."""

from __future__ import annotations

import decimal

import pytest

from app.errors import ValidationError
from app.services import settings_service


def test_default_cost_per_drawer_is_seeded(db_session):
    # db_session fixture already runs seed_master_data, which seeds this setting.
    assert settings_service.get_cost_per_drawer(db_session) == decimal.Decimal("35.00")


def test_set_cost_per_drawer_updates_and_persists(db_session):
    updated = settings_service.set_cost_per_drawer(db_session, decimal.Decimal("42.50"))
    assert updated == decimal.Decimal("42.50")
    assert settings_service.get_cost_per_drawer(db_session) == decimal.Decimal("42.50")


def test_set_cost_per_drawer_rejects_zero_or_negative(db_session):
    with pytest.raises(ValidationError):
        settings_service.set_cost_per_drawer(db_session, decimal.Decimal("0"))
    with pytest.raises(ValidationError):
        settings_service.set_cost_per_drawer(db_session, decimal.Decimal("-5"))


def test_get_cost_per_drawer_falls_back_when_row_missing(db_session):
    from app.models import AppSetting

    db_session.query(AppSetting).delete()
    db_session.commit()
    assert settings_service.get_cost_per_drawer(db_session) == (
        settings_service.DEFAULT_FALLBACK_COST_PER_DRAWER
    )
