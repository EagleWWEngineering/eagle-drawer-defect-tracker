"""SQLAlchemy ORM models. Persistence only — no business rules here.

Counting rules, validation, and status-transition rules live in
app/services/defect_service.py so the UI and MCP server share exactly one
implementation of the business logic (see AGENTS.md / CLAUDE.md).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
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
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
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
