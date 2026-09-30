"""add order_lines table and drawer identity columns on defect_cases

Revision ID: b8e3f0d5c2a1
Revises: a7d2e9c4b1f0
Create Date: 2026-09-30 10:00:00.000000

PROJECT_SPEC_PHASE10.md Part 2 - unique-ID drawer labels:

  - order_lines: the order_detail_id -> (order, line letter, qty, detail)
    mapping pushed hourly by eagle-drawers-production-count
    (POST /api/v1/sync/order-lines/ingest-raw). Keyed by the Access order-line
    record id (order_detail_id) itself - no surrogate key. Rows for orders
    absent from a later snapshot are kept forever (a defect can be logged
    after an order ships and leaves the open-orders snapshot).
  - defect_cases.order_detail_id / drawer_unit: which physical drawer a case
    is about, read from the label's "#drawer=<order_detail_id>-<unit>"
    fragment. Both NULLABLE - manual entry and old order-only labels have
    neither. No foreign key to order_lines on purpose: a label can be scanned
    before its line has been pushed, and the drawer identity is still worth
    keeping.

Purely additive - no existing column changes, no backfill.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e3f0d5c2a1"
down_revision: Union[str, None] = "a7d2e9c4b1f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "order_lines",
        sa.Column("order_detail_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("order_no", sa.String(length=20), nullable=False),
        sa.Column("line", sa.String(length=10), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=True),
        sa.Column("detail_json", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("order_detail_id"),
    )
    op.create_index("ix_order_lines_order_no", "order_lines", ["order_no"])

    op.add_column("defect_cases", sa.Column("order_detail_id", sa.Integer(), nullable=True))
    op.add_column("defect_cases", sa.Column("drawer_unit", sa.Integer(), nullable=True))
    op.create_index("ix_defect_cases_order_detail_id", "defect_cases", ["order_detail_id"])


def downgrade() -> None:
    op.drop_index("ix_defect_cases_order_detail_id", table_name="defect_cases")
    with op.batch_alter_table("defect_cases") as batch_op:
        batch_op.drop_column("drawer_unit")
        batch_op.drop_column("order_detail_id")
    op.drop_index("ix_order_lines_order_no", table_name="order_lines")
    op.drop_table("order_lines")
