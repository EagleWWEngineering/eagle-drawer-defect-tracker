"""add auth_sessions table (Phase 2 - single shared login)

Revision ID: b1e2c3d4f5a6
Revises: a3c7e4f19b02
Create Date: 2026-08-14 09:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1e2c3d4f5a6"
down_revision: Union[str, None] = "a3c7e4f19b02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("auth_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_auth_sessions_token"), ["token"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("auth_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_auth_sessions_token"))
    op.drop_table("auth_sessions")
