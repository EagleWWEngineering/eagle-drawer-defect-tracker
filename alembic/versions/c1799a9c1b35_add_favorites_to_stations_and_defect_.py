"""add favorites to stations and defect categories

Revision ID: c1799a9c1b35
Revises: 8f1c2d3a4b5e
Create Date: 2026-09-03 13:17:25.945622

Phase 3 (favorites) - two new columns on `stations` and `defect_categories`,
additive only: no drops, no backfill beyond the NOT NULL default below, no
change to any existing column, no touch to row data (name/active/sort_order
are untouched - see the seeding investigation this phase's prompt asked for,
which confirmed app/seed_data.py never overwrites an existing row's name and
this migration doesn't either).

  - is_favorite: NOT NULL, defaults false for every existing row via
    server_default - same pattern as resolved_on_the_spot/skipped_recheck
    (see a3c7e4f19b02). No station/category starts out favorited; Admin picks
    up to 5 per table (app/services/master_data_service.py enforces the max).
  - favorite_rank: nullable, only meaningful when is_favorite is true - orders
    the New Defect form's quick-pick bar. NULL for every existing row and for
    any non-favorited row going forward.

BEFORE running this against the live database: back up
/var/data/defect_tracker.db to /var/data/backups/ via Render Shell first -
backup_database.py only writes to the ephemeral filesystem, not the
persistent disk, so it alone isn't enough here (per this phase's prompt).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1799a9c1b35"
down_revision: Union[str, None] = "8f1c2d3a4b5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("stations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(sa.Column("favorite_rank", sa.Integer(), nullable=True))

    with op.batch_alter_table("defect_categories", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(sa.Column("favorite_rank", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("defect_categories", schema=None) as batch_op:
        batch_op.drop_column("favorite_rank")
        batch_op.drop_column("is_favorite")

    with op.batch_alter_table("stations", schema=None) as batch_op:
        batch_op.drop_column("favorite_rank")
        batch_op.drop_column("is_favorite")
