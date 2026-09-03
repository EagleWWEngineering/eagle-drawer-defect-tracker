"""Unit tests: the seed-duplicate fix (seed_key). seed_master_data() previously
decided "this default station/category already exists" purely by matching the
CURRENT name - renaming a row away from its default name made that name vanish
from the check, so the next call (every app startup) silently inserted a fresh
duplicate row under the old default name. Confirmed fired in production
(~12 stray duplicate defect categories, 2026-09-03) before this fix.

Uses the shared `db_session` fixture (tests/conftest.py), which already calls
seed_master_data() once - every test here calls it again to simulate a second
app startup after some change, which is exactly the failure mode."""

from __future__ import annotations

from app.models import DefectCategory, Station
from app.seed_data import DEFECT_CATEGORIES, STATIONS
from app.seed_data import seed_master_data as reseed


def test_reseeding_with_no_changes_creates_nothing(db_session):
    station_count_before = db_session.query(Station).count()
    category_count_before = db_session.query(DefectCategory).count()

    reseed(db_session)

    assert db_session.query(Station).count() == station_count_before
    assert db_session.query(DefectCategory).count() == category_count_before


def test_fresh_seed_sets_seed_key_to_the_default_name(db_session):
    station = db_session.query(Station).filter(Station.name == "Dado").one()
    assert station.seed_key == "Dado"

    category = (
        db_session.query(DefectCategory).filter(DefectCategory.name == "Dado / Bottom Groove").one()
    )
    assert category.seed_key == "Dado / Bottom Groove"


def test_renaming_a_station_does_not_recreate_it_on_reseed(db_session):
    station = db_session.query(Station).filter(Station.name == "Dado").one()
    station.name = "CNC Dado (renamed)"
    db_session.commit()
    count_before = db_session.query(Station).count()

    reseed(db_session)

    assert db_session.query(Station).count() == count_before
    assert db_session.query(Station).filter(Station.name == "Dado").first() is None
    still_renamed = db_session.query(Station).filter(Station.name == "CNC Dado (renamed)").one()
    assert still_renamed.seed_key == "Dado", "seed_key must survive the rename untouched"


def test_renaming_a_category_does_not_recreate_it_on_reseed(db_session):
    category = (
        db_session.query(DefectCategory).filter(DefectCategory.name == "Dado / Bottom Groove").one()
    )
    category.name = "Bottom Groove (renamed by admin)"
    db_session.commit()
    count_before = db_session.query(DefectCategory).count()

    reseed(db_session)

    assert db_session.query(DefectCategory).count() == count_before
    assert (
        db_session.query(DefectCategory)
        .filter(DefectCategory.name == "Dado / Bottom Groove")
        .first()
        is None
    )


def test_production_incident_reproduced_and_fixed(db_session):
    """Exact sequence that fired in production, reconstructed:
    1. A category gets renamed (admin's deliberate edit).
    2. Before this fix existed, a later restart's reseed would have inserted a
       stray duplicate under the old default name - reproduced here by hand
       (a pre-fix duplicate has seed_key=NULL, since it was never created by
       the fixed _seed_missing()).
    3. The stray duplicate gets deactivated by hand (never deleted).
    4. A subsequent restart's reseed must NOT insert a third row, and must
       NOT touch either existing row (the renamed original or the deactivated
       duplicate) in any way - not name, not active, not seed_key.
    """
    original = (
        db_session.query(DefectCategory).filter(DefectCategory.name == "Dado / Bottom Groove").one()
    )
    original.name = "Bottom Groove Custom"
    db_session.commit()

    stray_duplicate = DefectCategory(
        name="Dado / Bottom Groove", active=True, sort_order=99, seed_key=None
    )
    db_session.add(stray_duplicate)
    db_session.commit()

    stray_duplicate.active = False
    db_session.commit()
    duplicate_id = stray_duplicate.id
    count_before = db_session.query(DefectCategory).count()

    reseed(db_session)

    assert (
        db_session.query(DefectCategory).count() == count_before
    ), "reseeding after the incident must not create a third row"

    db_session.refresh(original)
    assert original.name == "Bottom Groove Custom"
    assert original.active is True
    assert original.seed_key == "Dado / Bottom Groove"

    duplicate = db_session.get(DefectCategory, duplicate_id)
    assert duplicate.name == "Dado / Bottom Groove"
    assert duplicate.active is False, "a deactivated stray duplicate must never be reactivated"
    assert duplicate.seed_key is None, "a deactivated row must never be backfilled with a seed_key"


def test_every_default_name_is_represented_exactly_once(db_session):
    """Sanity check on the fixture's baseline state - one row per default name,
    no accidental duplication from the fixture itself."""
    station_names = [s.name for s in db_session.query(Station).all()]
    assert len(station_names) == len(set(station_names))
    for name in STATIONS:
        assert name in station_names

    category_names = [c.name for c in db_session.query(DefectCategory).all()]
    assert len(category_names) == len(set(category_names))
    for name in DEFECT_CATEGORIES:
        assert name in category_names
