"""drop ck_rejected_le_inspected from daily_production_summaries

Revision ID: a7d2e9c4b1f0
Revises: 4e9b5dea94ec
Create Date: 2026-09-30 09:00:00.000000

PROJECT_SPEC_PHASE10.md Part 1 - drawers_inspected is now written only by
the eagle-drawers-production-count feed (unique valid QC scans per day), while
drawers_rejected_unique stays a hand-entered count from defect cases. They are
independent KPIs from independent sources, so "rejected <= inspected" is no
longer a rule the data can be expected to satisfy (e.g. the form is saved
before the day's 15:30 feed has arrived, or a QC undo lowers the count after
rejections were logged).

SQLite can't drop a constraint in place, so this uses a batch (table-rebuild)
migration. Every other CHECK constraint on the table (the four *_nonneg
checks) and the (production_date, shift) unique constraint are kept - only
ck_rejected_le_inspected is dropped. No data changes.

Downgrade re-adds the constraint; it will fail if any row by then has
drawers_rejected_unique > drawers_inspected, which is the correct outcome
(the old rule can't be restored over data that breaks it).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d2e9c4b1f0"
down_revision: Union[str, None] = "4e9b5dea94ec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("daily_production_summaries", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_rejected_le_inspected", type_="check")


def downgrade() -> None:
    with op.batch_alter_table("daily_production_summaries", recreate="always") as batch_op:
        batch_op.create_check_constraint(
            "ck_rejected_le_inspected", "drawers_rejected_unique <= drawers_inspected"
        )
