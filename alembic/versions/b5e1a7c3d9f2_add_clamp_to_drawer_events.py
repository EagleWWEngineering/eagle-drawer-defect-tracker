"""add clamp to drawer_events

Revision ID: b5e1a7c3d9f2
Revises: a4d0f6b2c8e1
Create Date: 2026-10-06 19:00:00.000000

2026-10 redesign: production count now names the clamp that assembled each
drawer on every drawer event (optional "clamp"). Nullable - every event received
before this, and every drawer that never went through a known clamp, has none.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5e1a7c3d9f2"
down_revision: Union[str, None] = "a4d0f6b2c8e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("drawer_events") as batch:
        batch.add_column(sa.Column("clamp", sa.String(length=40), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("drawer_events") as batch:
        batch.drop_column("clamp")
