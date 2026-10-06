"""add work_orders table (customer name per order)

Revision ID: d1a7c3e9f2b4
Revises: c9f4a1e6d3b2
Create Date: 2026-10-06 15:00:00.000000

2026-10 redesign: production count now sends each order's customer with the
order-line snapshot (POST /api/v1/sync/order-lines/ingest-raw, optional
"customer" per order). Purely additive - no existing table changes.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1a7c3e9f2b4"
down_revision: Union[str, None] = "c9f4a1e6d3b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "work_orders",
        sa.Column("order_no", sa.String(length=20), nullable=False),
        sa.Column("customer_name", sa.String(length=120), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("order_no"),
    )


def downgrade() -> None:
    op.drop_table("work_orders")
