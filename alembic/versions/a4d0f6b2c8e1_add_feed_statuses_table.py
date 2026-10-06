"""add feed_statuses table (feed health)

Revision ID: a4d0f6b2c8e1
Revises: f3c9e5a1b4d6
Create Date: 2026-10-06 18:00:00.000000

2026-10 redesign: one row per production-count feed - when it last arrived and
whether it was accepted (app/services/feed_health_service.py). Purely additive.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4d0f6b2c8e1"
down_revision: Union[str, None] = "f3c9e5a1b4d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feed_statuses",
        sa.Column("feed", sa.String(length=40), nullable=False),
        sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result", sa.String(length=10), nullable=False),
        sa.Column("last_message", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("feed"),
    )


def downgrade() -> None:
    op.drop_table("feed_statuses")
