"""Business rules for master data (stations, defect categories).

Station/DefectCategory field edits (name/active/sort_order) were simple enough to
live directly in app/routers/master_data.py until now; Phase 3's favorites max-5
enforcement is real business logic that must not be bypassable by calling the API
directly (PROJECT_SPEC.md architecture rule), so both that plain field logic and
the new favorites rule now live here together, in one service-layer entry point
per entity, rather than splitting where the rule lives from where the router
already was.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.errors import NotFoundError, ValidationError
from app.models import DefectCategory, Station

MAX_FAVORITES = 5


def _next_favorite_rank(db: Session, model: type[Station] | type[DefectCategory]) -> int:
    """First free slot in 1..MAX_FAVORITES, by ACTIVE favorited rows only - see
    _apply_favorite's docstring for why active-only. Not expected to ever run out
    (the max-5 check in _apply_favorite always runs first), but never divides by
    a missing slot if it somehow does."""
    used = {
        rank
        for (rank,) in db.query(model.favorite_rank)
        .filter(
            model.is_favorite.is_(True), model.active.is_(True), model.favorite_rank.isnot(None)
        )
        .all()
    }
    for candidate in range(1, MAX_FAVORITES + 1):
        if candidate not in used:
            return candidate
    raise ValidationError(f"All {MAX_FAVORITES} favorite slots are taken.", field="is_favorite")


def _apply_favorite(
    db: Session,
    model: type[Station] | type[DefectCategory],
    row: Station | DefectCategory,
    *,
    is_favorite: bool,
    entity_label: str,
) -> None:
    """Phase 3 guardrail, enforced here (not just in the Admin UI): setting a 6th
    favorite is rejected outright rather than silently bumping the oldest one off
    - that would be a surprising, hard-to-audit behavior. An explicit unfavorite
    is required first.

    The 5-cap (and the count Admin's "X/5 favorited" reads) is scoped to ACTIVE
    favorited rows only, deliberately: a station stays flagged is_favorite while
    deactivated (reactivating it later just brings it straight back to the
    quick-pick bar, no re-favoriting step needed - "active status still wins" per
    the New Defect page rule, applied the same way here), so counting it toward
    the cap would let an invisible, deactivated row permanently occupy one of the
    5 slots with no obvious way for Admin to see why a 4th active favorite is
    being rejected.
    """
    if is_favorite and not row.is_favorite:
        active_favorite_count = (
            db.query(model).filter(model.is_favorite.is_(True), model.active.is_(True)).count()
        )
        if active_favorite_count >= MAX_FAVORITES:
            raise ValidationError(
                f"Already at {MAX_FAVORITES} favorited {entity_label} - unfavorite " "one first.",
                field="is_favorite",
            )
        row.is_favorite = True
        row.favorite_rank = _next_favorite_rank(db, model)
    elif not is_favorite:
        row.is_favorite = False
        # favorite_rank is deliberately left as-is, not cleared - see the
        # Station/DefectCategory model docstrings in app/models.py.
    # is_favorite True and already favorite: no-op, nothing to enforce or change.


def update_station(
    db: Session,
    station_id: int,
    *,
    name: str | None = None,
    active: bool | None = None,
    sort_order: int | None = None,
    is_favorite: bool | None = None,
) -> Station:
    station = db.get(Station, station_id)
    if station is None:
        raise NotFoundError(f"Station {station_id} not found.")
    if name is not None:
        station.name = name.strip()
    if active is not None:
        station.active = active
    if sort_order is not None:
        station.sort_order = sort_order
    if is_favorite is not None:
        _apply_favorite(db, Station, station, is_favorite=is_favorite, entity_label="stations")
    db.commit()
    db.refresh(station)
    return station


def update_category(
    db: Session,
    category_id: int,
    *,
    name: str | None = None,
    active: bool | None = None,
    sort_order: int | None = None,
    is_favorite: bool | None = None,
) -> DefectCategory:
    category = db.get(DefectCategory, category_id)
    if category is None:
        raise NotFoundError(f"Defect category {category_id} not found.")
    if name is not None:
        category.name = name.strip()
    if active is not None:
        category.active = active
    if sort_order is not None:
        category.sort_order = sort_order
    if is_favorite is not None:
        _apply_favorite(
            db, DefectCategory, category, is_favorite=is_favorite, entity_label="defect categories"
        )
    db.commit()
    db.refresh(category)
    return category


# --- Delete / restore (2026-10 redesign) ------------------------------------------
#
# Admin's Delete only HIDES a station or category (is_deleted): the row stays, so
# every historical case still shows its name and app/seed_data.py's seed loop still
# finds the name and never re-creates it - CLAUDE.md: never hard-delete master
# data, not even an unused row. Deleted rows drop out of every picker and of
# Admin's normal lists, and come back with Restore.


def _protected_station_ids(db: Session) -> dict[int, str]:
    """Stations the UNDO-card kickbacks open their cases at - deleting one would
    make every kickback in that area fail."""
    from app.services.drawer_event_service import AREA_STATIONS, _seeded

    out: dict[int, str] = {}
    for area, name in AREA_STATIONS.items():
        station = _seeded(db, Station, name)
        if station is not None:
            out[station.id] = f"UNDO-card kickbacks ({area}) are recorded at it"
    return out


def _protected_category_ids(db: Session) -> dict[int, str]:
    """The kickback categories chosen in Admin, plus the fallback category."""
    from app.services import settings_service
    from app.services.drawer_event_service import FALLBACK_CATEGORY, _seeded

    out: dict[int, str] = {}
    for area in settings_service.UNDO_CATEGORY_SETTING_KEYS:
        category_id = settings_service.get_undo_category_id(db, area)
        if category_id is not None:
            area_name = {"qc": "QC", "assembly": "Assembly In"}.get(area, area)
            out[category_id] = f"it is the {area_name} kickback category (Admin > UNDO Card)"
    fallback = _seeded(db, DefectCategory, FALLBACK_CATEGORY)
    if fallback is not None:
        out.setdefault(fallback.id, "kickbacks fall back to it when no category is set")
    return out


def _get_row(db: Session, model: type[Station] | type[DefectCategory], row_id: int, label: str):
    row = db.get(model, row_id)
    if row is None:
        raise NotFoundError(f"{label} {row_id} not found.")
    return row


def delete_master_row(
    db: Session, model: type[Station] | type[DefectCategory], row_id: int
) -> Station | DefectCategory:
    is_station = model is Station
    label = "Station" if is_station else "Defect category"
    row = _get_row(db, model, row_id, label)
    protected = _protected_station_ids(db) if is_station else _protected_category_ids(db)
    if row.id in protected:
        raise ValidationError(f"Can't delete '{row.name}': {protected[row.id]}.")
    if not row.is_deleted:
        row.is_deleted = True
        row.deleted_at = dt.datetime.now(dt.timezone.utc)
        row.is_favorite = False
        db.commit()
        db.refresh(row)
    return row


def restore_master_row(
    db: Session, model: type[Station] | type[DefectCategory], row_id: int
) -> Station | DefectCategory:
    label = "Station" if model is Station else "Defect category"
    row = _get_row(db, model, row_id, label)
    if row.is_deleted:
        row.is_deleted = False
        row.deleted_at = None
        db.commit()
        db.refresh(row)
    return row
