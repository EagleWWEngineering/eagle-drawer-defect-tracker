"""soft delete for stations and defect categories

Revision ID: e2b8d4f0a3c5
Revises: d1a7c3e9f2b4
Create Date: 2026-10-06 16:00:00.000000

2026-10 redesign: Admin gets Delete (and Restore) for stations and defect
categories. A delete only hides the row (is_deleted): it stays in the table so
historical cases keep their names and app/seed_data.py's seed loop still sees the
name and never recreates it (CLAUDE.md: never hard-delete master data). Batch
mode for SQLite; existing rows default to not deleted.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2b8d4f0a3c5"
down_revision: Union[str, None] = "d1a7c3e9f2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("stations", "defect_categories")


def upgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false())
            )
            batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("deleted_at")
            batch.drop_column("is_deleted")
