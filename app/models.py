"""SQLAlchemy ORM models. Persistence only — no business rules here.

Counting rules, validation, and status-transition rules live in
app/services/defect_service.py so the UI and MCP server share exactly one
implementation of the business logic (see AGENTS.md / CLAUDE.md).
"""

from __future__ import annotations

import datetime as dt
import decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Station(Base):
    """A production step, e.g. 'Dado' or 'QC / Sorting / Shipping'."""

    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DefectCategory(Base):
    """A defect classification, e.g. 'Sanding / Surface' or 'Dado / Bottom Groove'."""

    __tablename__ = "defect_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DailyProductionSummary(Base):
    """One row per production date + shift: the denominators for every rate."""

    __tablename__ = "daily_production_summaries"
    __table_args__ = (
        UniqueConstraint("production_date", "shift", name="uq_daily_summary_date_shift"),
        CheckConstraint("drawers_inspected >= 0", name="ck_inspected_nonneg"),
        CheckConstraint("drawers_rejected_unique >= 0", name="ck_rejected_nonneg"),
        CheckConstraint("drawers_reworked >= 0", name="ck_reworked_nonneg"),
        CheckConstraint("drawers_scrapped >= 0", name="ck_scrapped_nonneg"),
        CheckConstraint(
            "drawers_rejected_unique <= drawers_inspected", name="ck_rejected_le_inspected"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    production_date: Mapped[dt.date] = mapped_column(nullable=False, index=True)
    shift: Mapped[str] = mapped_column(String(40), nullable=False, default="Day")
    drawers_inspected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    drawers_rejected_unique: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    drawers_reworked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    drawers_scrapped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Snapshot of the active cost_per_drawer AppSetting at save time (Phase 4 cost
    # tracking). Nullable because rows created before this feature existed have no
    # snapshot - see app/services/metrics_service.py for how that's handled in
    # cost calculations. Never recomputed retroactively when the rate changes later.
    cost_per_drawer_at_time: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DailySchedule(Base):
    """One row per calendar date: how many drawers the production brief scheduled
    to finish that day (Phase 6 - see docs/PRODUCTION_BRIEF_SCHEDULE_SOURCE.md and
    the PROJECT_SPEC.md Phase 6 addendum).

    Deliberately a separate table from DailyProductionSummary, not a column on it:
    DailyProductionSummary is unique on (production_date, shift), but "scheduled"
    is a whole-day figure - a two-shift day would either double-count on SUM() or
    force every query to dedupe by date. Keying this table by production_date alone
    makes the correct query the easy query.

    source distinguishes where the value came from:
      - "sync": written by the relay ingest endpoint from the production brief.
      - "manual": a human typed/edited it on the Daily Production Summary form.
    Manual-wins: once a date's row has source="manual", the sync must skip that
    date entirely (see app/services/schedule_service.py upsert_schedule) until a
    human clears it - a later sync must never silently overwrite a human's number.
    """

    __tablename__ = "daily_schedules"
    __table_args__ = (
        CheckConstraint("drawers_scheduled >= 0", name="ck_schedule_nonneg"),
        CheckConstraint("source IN ('sync', 'manual')", name="ck_schedule_source"),
    )

    production_date: Mapped[dt.date] = mapped_column(primary_key=True)
    drawers_scheduled: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    # Last successful relay write, UTC. Null for a row that has never been synced
    # (a pure manual entry with no prior sync). Not updated by a manual edit - that
    # only touches source/updated_at - so it still reflects the last time the relay
    # actually wrote something, even if a human has since overridden the value.
    synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DefectCase(Base):
    """One QC finding for one work order: header for one or more DefectItems."""

    __tablename__ = "defect_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, index=True)
    production_date: Mapped[dt.date] = mapped_column(nullable=False, index=True)
    detected_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    work_order_number: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    drawer_part_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)

    found_station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), nullable=False)
    possible_source_station_id: Mapped[int | None] = mapped_column(
        ForeignKey("stations.id"), nullable=True
    )

    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="Normal")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="Open")
    disposition: Mapped[str | None] = mapped_column(String(20), nullable=True)
    repair_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrective_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "Fixed immediately?" fast path (PROJECT_SPEC.md section 3.3): true when this
    # case was created already in a closed status because the QC catch was resolved
    # in the same ~60 seconds as entry, instead of going through Open/In Rework/Ready
    # for QC Recheck. Set once at creation, never changed afterward - see
    # app/services/defect_service.py create_defect_case.
    resolved_on_the_spot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # "Close Directly (Skip Recheck)" fast path (PROJECT_SPEC.md section 3.3): true
    # when this case went In Rework -> Closed-* directly, bypassing Ready for QC
    # Recheck. Set by app/services/defect_service.py update_case_status.
    skipped_recheck: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    found_station: Mapped[Station] = relationship(foreign_keys=[found_station_id])
    possible_source_station: Mapped[Station | None] = relationship(
        foreign_keys=[possible_source_station_id]
    )
    items: Mapped[list["DefectItem"]] = relationship(
        back_populates="defect_case", cascade="all, delete-orphan"
    )
    photos: Mapped[list["DefectPhoto"]] = relationship(
        back_populates="defect_case", cascade="all, delete-orphan"
    )
    status_history: Mapped[list["StatusHistory"]] = relationship(
        back_populates="defect_case",
        cascade="all, delete-orphan",
        order_by="StatusHistory.changed_at",
    )


class DefectItem(Base):
    """One category on one case, with the affected drawer quantity for that category."""

    __tablename__ = "defect_items"
    __table_args__ = (
        UniqueConstraint("defect_case_id", "defect_category_id", name="uq_case_category"),
        CheckConstraint("affected_drawer_quantity >= 1", name="ck_qty_at_least_1"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    defect_case_id: Mapped[int] = mapped_column(
        ForeignKey("defect_cases.id", ondelete="CASCADE"), nullable=False
    )
    defect_category_id: Mapped[int] = mapped_column(
        ForeignKey("defect_categories.id"), nullable=False
    )
    affected_drawer_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    defect_case: Mapped[DefectCase] = relationship(back_populates="items")
    defect_category: Mapped[DefectCategory] = relationship()


class DefectPhoto(Base):
    """Metadata for an optional photo attached to a defect case."""

    __tablename__ = "defect_photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    defect_case_id: Mapped[int] = mapped_column(
        ForeignKey("defect_cases.id", ondelete="CASCADE"), nullable=False
    )
    stored_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    defect_case: Mapped[DefectCase] = relationship(back_populates="photos")


class StatusHistory(Base):
    """Audit trail of every status change on a defect case."""

    __tablename__ = "status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    defect_case_id: Mapped[int] = mapped_column(
        ForeignKey("defect_cases.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    defect_case: Mapped[DefectCase] = relationship(back_populates="status_history")


class AuditLog(Base):
    """Append-only log of every create/edit/status-change/delete/export/MCP-write."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor_role: Mapped[str] = mapped_column(String(40), nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    inputs_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class CustomerIssueCategory(Base):
    """A customer-complaint classification, e.g. 'Wrong Size' or 'Finish Quality'.

    Deliberately a separate table from DefectCategory (PROJECT_SPEC_PHASE2.md) - a
    customer complaint category is not the same vocabulary as an internal QC defect
    category, even though a customer issue can later be linked to an internal case.
    """

    __tablename__ = "customer_issue_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CustomerIssue(Base):
    """A customer-reported quality complaint, synced hourly from the production
    brief's JSON API (see docs/PROJECT_SPEC_PHASE3.md and app/services/sync_service.py).

    source_thread_id is the dedup key: rows with it set were created/updated by the
    sync; rows with it null were entered manually through the UI (phone/walk-in
    reports) and are never touched by sync.

    Kept entirely separate from DefectCase: a customer issue may optionally be
    *linked* to an internal defect case (linked_defect_case_id) once QC confirms the
    connection, but it is never merged into DefectCase/DefectItem rows - the counting
    rules for internal defect events (PROJECT_SPEC.md section 2) are unaffected by
    customer issues existing.
    """

    __tablename__ = "customer_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, index=True)
    source_thread_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    reported_date: Mapped[dt.date] = mapped_column(nullable=False, index=True)
    customer_name: Mapped[str] = mapped_column(String(120), nullable=False)
    order_number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    issue_category_id: Mapped[int] = mapped_column(
        ForeignKey("customer_issue_categories.id"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    should_have_caught_at: Mapped[str | None] = mapped_column(String(120), nullable=True)
    piece_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    estimated_rework_cost: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    photo_urls: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="Open")
    linked_defect_case_id: Mapped[int | None] = mapped_column(
        ForeignKey("defect_cases.id"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        CheckConstraint("piece_count >= 1", name="ck_customer_issue_piece_count_at_least_1"),
        CheckConstraint(
            "source_type IN ('Manufacturing', 'Shipping Damage')",
            name="ck_customer_issue_source_type",
        ),
        CheckConstraint("status IN ('Open', 'Ignored', 'Linked')", name="ck_customer_issue_status"),
    )

    issue_category: Mapped[CustomerIssueCategory] = relationship()
    linked_defect_case: Mapped[DefectCase | None] = relationship(
        foreign_keys=[linked_defect_case_id]
    )


class SyncLog(Base):
    """One row per production-brief sync attempt (app/services/sync_service.py).

    This is the audit trail for the sync feature specifically - separate from
    AuditLog, which covers user/MCP-driven writes. Every sync attempt gets a row,
    whether it succeeds or fails, so Admin can see a history of what happened.
    """

    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sync_completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_url: Mapped[str] = mapped_column(String(255), nullable=False)
    records_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="failed")


class AuthSession(Base):
    """A server-side login session (Phase 2 - single shared login).

    Deliberately has no `expires_at`/TTL column: sessions never expire on their own
    (see app/services/auth_service.py) - a row here is valid for as long as it
    exists, however old `created_at` gets. The ONLY ways a row disappears are an
    explicit "Log out" (deletes just this row) or "Log out everywhere" (deletes
    every row). There is no separate `User` table because this app has exactly one
    shared username/password for the whole app, not per-user accounts - the cookie
    just proves "this browser knows the one shared password", nothing more.
    """

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSetting(Base):
    """Generic key-value application settings (Phase 4), editable on the Admin
    screen. Starts with just "cost_per_drawer" but is intentionally generic so a
    future setting doesn't need its own table/migration.

    Values are stored as strings and parsed by the reading service
    (app/services/settings_service.py) - keeps this table dead simple.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
