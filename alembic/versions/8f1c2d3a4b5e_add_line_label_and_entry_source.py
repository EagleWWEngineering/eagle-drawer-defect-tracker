"""add line_label and entry_source to defect_cases

Revision ID: 8f1c2d3a4b5e
Revises: 7c1f9a2b4e6d
Create Date: 2026-09-01 09:00:00.000000

PROJECT_SPEC_PHASE9.md Part 1 - two new NULLABLE columns on defect_cases, no
drops, no backfill of existing rows, no changes to existing columns:

  - line_label: the work order line this case belongs to (A-Z, AA-ZZ),
    normalised uppercase/whitespace-stripped on save by
    app/services/defect_service.py before it ever reaches this column. No
    unique constraint and no foreign key - there is no work order table to
    reference, and one line legitimately appears on many cases. Indexed
    alongside work_order_number (the pair is how it's queried - see
    app/routers/defect_cases.py list_cases) rather than on its own.
  - entry_source: "manual" / "scanned" / "scanned_edited" - how this case's
    line (and QR-filled order number) got onto the form. No CHECK constraint
    deliberately: this is a soft analytics signal ("did scanning actually get
    used"), not a business rule to enforce at the schema level, same
    discretion the rest of this app already applies to disposition.

Existing rows get NULL for both columns and are never backfilled - there is no
way to infer a historical case's work order line from data that was never
captured.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f1c2d3a4b5e"
down_revision: Union[str, None] = "7c1f9a2b4e6d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("defect_cases", sa.Column("line_label", sa.String(length=10), nullable=True))
    op.add_column("defect_cases", sa.Column("entry_source", sa.String(length=20), nullable=True))
    op.create_index(
        "ix_defect_cases_work_order_line",
        "defect_cases",
        ["work_order_number", "line_label"],
    )


def downgrade() -> None:
    op.drop_index("ix_defect_cases_work_order_line", table_name="defect_cases")
    op.drop_column("defect_cases", "entry_source")
    op.drop_column("defect_cases", "line_label")
