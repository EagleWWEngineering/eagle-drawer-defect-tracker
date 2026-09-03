"""add seed_key to stations and defect categories

Revision ID: 4e9b5dea94ec
Revises: c1799a9c1b35
Create Date: 2026-09-03 14:02:24.392999

Seed-duplicate fix, step 1. app/seed_data.py seed_master_data() decided "this
default station/category already exists" purely by matching the CURRENT name -
renaming a row away from its default name made that name vanish from the
"already exists" check, so the next restart silently re-inserted a fresh
duplicate row under the old default name. Confirmed fired in production:
~12 stray duplicate defect categories were created under original default
names on a 2026-09-03 restart, sitting alongside the admin's renamed rows;
those duplicates were deactivated by hand (never deleted - already-referenced
master data is never hard-deleted per this app's rule) before this migration
was written.

seed_key is a new, nullable column: a durable, rename-proof marker recording
the ORIGINAL default name a row was created under, set once at insert time and
never touched by an Admin edit (only app/seed_data.py ever writes it, going
forward). The seeding loop's match, from this point on, is "current name
already present" OR "seed_key already present" - the old name-only check stays
exactly as it was (it's what already correctly skips re-inserting a default
whose row exists but is inactive, including this incident's freshly-
deactivated stray duplicates - an inactive row is never touched here just for
being inactive); seed_key is added ON TOP, purely to also catch the "renamed
away" case name-matching alone can't see.

Backfill below touches ONLY currently-ACTIVE rows whose name still exactly
matches one of the default lists frozen here (as they stood at the time of
writing - deliberately NOT imported from app/seed_data.py, so this migration
replays identically regardless of any later change to that file). Every other
row - every rename, and explicitly every currently-inactive row, including
this incident's ~12 stray duplicates - gets seed_key = NULL and is otherwise
completely untouched: no name/active/sort_order change, no reactivation,
nothing. This is deliberate, not an oversight: a human is reviewing the real
data (see the "Created" column added to Admin in the prior deploy) before any
further cleanup decision gets made on a per-row basis.

Scope: stations and defect_categories only, matching the reported incident.
customer_issue_categories has the identical seed-loop shape and the same
theoretical exposure, but is not touched here - flagged as a separate,
not-yet-decided follow-up, not silently included.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4e9b5dea94ec"
down_revision: Union[str, None] = "c1799a9c1b35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen snapshot of app/seed_data.py's STATIONS / DEFECT_CATEGORIES as they
# stood when this migration was written - see the module docstring above for
# why this is a copy, not an import.
SEED_STATIONS = (
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
)
SEED_DEFECT_CATEGORIES = (
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
)


def upgrade() -> None:
    with op.batch_alter_table("stations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("seed_key", sa.String(length=120), nullable=True))
    with op.batch_alter_table("defect_categories", schema=None) as batch_op:
        batch_op.add_column(sa.Column("seed_key", sa.String(length=120), nullable=True))

    bind = op.get_bind()
    stations = sa.table(
        "stations",
        sa.column("name", sa.String),
        sa.column("active", sa.Boolean),
        sa.column("seed_key", sa.String),
    )
    defect_categories = sa.table(
        "defect_categories",
        sa.column("name", sa.String),
        sa.column("active", sa.Boolean),
        sa.column("seed_key", sa.String),
    )

    station_result = bind.execute(
        stations.update()
        .where(stations.c.active.is_(True), stations.c.name.in_(SEED_STATIONS))
        .values(seed_key=stations.c.name)
    )
    category_result = bind.execute(
        defect_categories.update()
        .where(
            defect_categories.c.active.is_(True),
            defect_categories.c.name.in_(SEED_DEFECT_CATEGORIES),
        )
        .values(seed_key=defect_categories.c.name)
    )
    # Surfaced in the deploy log (alembic upgrade head's stdout), same
    # discipline as the Phase 7 migration's row-count reporting.
    print(f"seed_key backfill: {station_result.rowcount} station row(s) touched.")
    print(f"seed_key backfill: {category_result.rowcount} defect category row(s) touched.")


def downgrade() -> None:
    with op.batch_alter_table("defect_categories", schema=None) as batch_op:
        batch_op.drop_column("seed_key")
    with op.batch_alter_table("stations", schema=None) as batch_op:
        batch_op.drop_column("seed_key")
