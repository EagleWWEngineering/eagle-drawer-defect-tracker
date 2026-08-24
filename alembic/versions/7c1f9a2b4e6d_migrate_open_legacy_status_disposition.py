"""migrate open legacy status/disposition to the Phase 7 vocabulary

Revision ID: 7c1f9a2b4e6d
Revises: 3d8532f3a9ec
Create Date: 2026-08-21 16:05:00.000000

PROJECT_SPEC_PHASE7.md, Change 3 - the ONE data change this phase makes. Only
currently-OPEN (non-closed) cases are touched:

  - status In Rework / Waiting / Ready for QC Recheck -> Open, with a real
    status_history row (from_status = the old value, to_status = "Open") and an
    audit_log row, so the trail of what the case used to be stays readable.
  - disposition Hold / Scrap / Use As Is (on any of those same non-closed cases,
    whatever their status) -> Set Aside, logged the same way.

Closed cases are NEVER touched here - not their status, not their disposition,
not "Closed - Scrapped", nothing. This migration only ever UPDATEs a status/
disposition value and INSERTs new status_history/audit_log rows; it never
deletes or drops anything.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c1f9a2b4e6d"
down_revision: Union[str, None] = "3d8532f3a9ec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RETIRED_STATUSES = ("In Rework", "Waiting", "Ready for QC Recheck")
RETIRED_DISPOSITIONS = ("Hold", "Scrap", "Use As Is")
CLOSED_STATUSES = ("Closed - Repaired", "Closed - Scrapped", "Closed - Use As Is")

STATUS_MIGRATION_NOTE = (
    "Phase 7 schema migration: this case's status was retired and simplified to "
    "Open (see docs/PROJECT_SPEC_PHASE7.md)."
)


def _tables():
    defect_cases = sa.table(
        "defect_cases",
        sa.column("id", sa.Integer),
        sa.column("case_number", sa.String),
        sa.column("status", sa.String),
        sa.column("disposition", sa.String),
        sa.column("updated_at", sa.DateTime),
    )
    status_history = sa.table(
        "status_history",
        sa.column("id", sa.Integer),
        sa.column("defect_case_id", sa.Integer),
        sa.column("from_status", sa.String),
        sa.column("to_status", sa.String),
        sa.column("note", sa.Text),
        sa.column("changed_at", sa.DateTime),
    )
    audit_log = sa.table(
        "audit_log",
        sa.column("id", sa.Integer),
        sa.column("timestamp", sa.DateTime),
        sa.column("actor_role", sa.String),
        sa.column("action", sa.String),
        sa.column("entity_type", sa.String),
        sa.column("entity_id", sa.String),
        sa.column("inputs_json", sa.Text),
        sa.column("before_json", sa.Text),
        sa.column("after_json", sa.Text),
        sa.column("success", sa.Boolean),
        sa.column("message", sa.Text),
    )
    return defect_cases, status_history, audit_log


def upgrade() -> None:
    bind = op.get_bind()
    defect_cases, status_history, audit_log = _tables()
    now = dt.datetime.now(dt.timezone.utc)

    rows = bind.execute(
        sa.select(
            defect_cases.c.id,
            defect_cases.c.case_number,
            defect_cases.c.status,
            defect_cases.c.disposition,
        ).where(defect_cases.c.status.notin_(CLOSED_STATUSES))
    ).fetchall()

    status_counts: dict[str, int] = {}
    disposition_counts: dict[str, int] = {}
    cases_touched = 0

    for row in rows:
        old_status = row.status
        old_disposition = row.disposition
        needs_status_change = old_status in RETIRED_STATUSES
        needs_disposition_change = old_disposition in RETIRED_DISPOSITIONS
        if not (needs_status_change or needs_disposition_change):
            continue

        new_status = "Open" if needs_status_change else old_status
        new_disposition = "Set Aside" if needs_disposition_change else old_disposition
        cases_touched += 1

        bind.execute(
            defect_cases.update()
            .where(defect_cases.c.id == row.id)
            .values(status=new_status, disposition=new_disposition, updated_at=now)
        )

        message_parts = []
        if needs_status_change:
            bind.execute(
                status_history.insert().values(
                    defect_case_id=row.id,
                    from_status=old_status,
                    to_status="Open",
                    note=STATUS_MIGRATION_NOTE,
                    changed_at=now,
                )
            )
            status_counts[old_status] = status_counts.get(old_status, 0) + 1
            message_parts.append(f"status {old_status!r} -> 'Open'")
        if needs_disposition_change:
            disposition_counts[old_disposition] = disposition_counts.get(old_disposition, 0) + 1
            message_parts.append(f"disposition {old_disposition!r} -> 'Set Aside'")

        bind.execute(
            audit_log.insert().values(
                timestamp=now,
                actor_role="system",
                action="update",
                entity_type="DefectCase",
                entity_id=row.case_number,
                inputs_json=None,
                before_json=json.dumps({"status": old_status, "disposition": old_disposition}),
                after_json=json.dumps({"status": new_status, "disposition": new_disposition}),
                success=True,
                message="Phase 7 migration: " + "; ".join(message_parts),
            )
        )

    # Surfaced in the deploy log (alembic upgrade head's stdout) - see the task's
    # "report the exact count of rows changed, by old value" requirement.
    print(f"Phase 7 migration: {cases_touched} case(s) touched.")
    print(f"Phase 7 migration: status changes by old value: {status_counts}")
    print(f"Phase 7 migration: disposition changes by old value: {disposition_counts}")


def downgrade() -> None:
    # Deliberately a no-op. This data migration is not meaningfully reversible:
    # multiple old statuses collapse onto the same "Open" value, so there is no
    # way to recover which specific legacy status a case used to have from the
    # data alone (the status_history rows this migration wrote ARE that record -
    # removing them to "undo" the migration would itself be the kind of
    # destructive rewrite PROJECT_SPEC_PHASE7.md's non-negotiable rule forbids).
    pass
