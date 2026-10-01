"""add drawer_events table

Revision ID: c9f4a1e6d3b2
Revises: b8e3f0d5c2a1
Create Date: 2026-10-01 10:00:00.000000

PROJECT_SPEC_PHASE11.md - UNDO-card kickbacks and auto-close on re-scan:
every drawer event production count pushes (POST
/api/v1/sync/drawer-events/ingest-raw) is kept here, unique per
(source, event_id), so a resend is never applied twice.

Purely additive - no existing table changes, no backfill.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9f4a1e6d3b2"
down_revision: Union[str, None] = "b8e3f0d5c2a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "drawer_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("area", sa.String(length=20), nullable=False),
        sa.Column("order_no", sa.String(length=20), nullable=False),
        sa.Column("order_detail_id", sa.Integer(), nullable=False),
        sa.Column("drawer_unit", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=255), nullable=False),
        sa.Column("defect_case_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["defect_case_id"], ["defect_cases.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "event_id", name="uq_drawer_event_source_id"),
    )
    op.create_index("ix_drawer_events_order_detail_id", "drawer_events", ["order_detail_id"])


def downgrade() -> None:
    op.drop_index("ix_drawer_events_order_detail_id", table_name="drawer_events")
    op.drop_table("drawer_events")
