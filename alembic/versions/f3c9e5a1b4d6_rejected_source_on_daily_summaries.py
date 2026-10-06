"""rejected_source on daily_production_summaries (automatic unique-rejected count)

Revision ID: f3c9e5a1b4d6
Revises: e2b8d4f0a3c5
Create Date: 2026-10-06 17:00:00.000000

2026-10 redesign: "Unique drawers rejected" no longer has to be typed every day.
rejected_source = 'auto' means the reports use the live count of that day's cases
(kickbacks included - defect_service.auto_rejected_by_date); 'manual' means the
number someone saved on Daily Summary wins.

Existing rows: 'manual', EXCEPT rows from 2026-09-30 on (the day the completed
count started coming from production count) that still hold 0 - those were
created by the feed and never filled in, so they become 'auto'. A typed number
is never overridden.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3c9e5a1b4d6"
down_revision: Union[str, None] = "e2b8d4f0a3c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("daily_production_summaries") as batch:
        batch.add_column(
            sa.Column(
                "rejected_source", sa.String(length=10), nullable=False, server_default="manual"
            )
        )
    op.execute(
        "UPDATE daily_production_summaries SET rejected_source = 'auto' "
        "WHERE production_date >= '2026-09-30' AND drawers_rejected_unique = 0"
    )


def downgrade() -> None:
    with op.batch_alter_table("daily_production_summaries") as batch:
        batch.drop_column("rejected_source")
