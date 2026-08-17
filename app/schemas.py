"""Pydantic v2 request/response models. Validation shape only — business rules live
in app/services/*.py so the same rules apply no matter which router calls them.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field, computed_field, field_validator

from app.services.defect_service import allowed_next_statuses, direct_close_statuses
from app.timezone_utils import to_display_string

# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


class StationOut(BaseModel):
    id: int
    name: str
    active: bool
    sort_order: int

    model_config = {"from_attributes": True}


class DefectCategoryOut(BaseModel):
    id: int
    name: str
    active: bool
    sort_order: int

    model_config = {"from_attributes": True}


class MasterDataOut(BaseModel):
    stations: list[StationOut]
    defect_categories: list[DefectCategoryOut]
    priorities: list[str]
    statuses: list[str]
    dispositions: list[str]


class StationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sort_order: int = 0


class StationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None
    sort_order: int | None = None


class DefectCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sort_order: int = 0


class DefectCategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None
    sort_order: int | None = None


# ---------------------------------------------------------------------------
# Defect cases
# ---------------------------------------------------------------------------


class DefectItemIn(BaseModel):
    defect_category_id: int
    affected_drawer_quantity: int = Field(default=1, ge=1)
    notes: str | None = None


class DefectItemOut(BaseModel):
    id: int
    defect_category_id: int
    defect_category_name: str
    affected_drawer_quantity: int
    notes: str | None

    model_config = {"from_attributes": True}


class StatusHistoryOut(BaseModel):
    id: int
    from_status: str | None
    to_status: str
    note: str | None
    changed_at: dt.datetime

    @computed_field
    @property
    def changed_at_local(self) -> str | None:
        return to_display_string(self.changed_at)

    model_config = {"from_attributes": True}


class DefectPhotoOut(BaseModel):
    id: int
    original_filename: str
    content_type: str
    uploaded_at: dt.datetime
    stored_filename: str

    @computed_field
    @property
    def url(self) -> str:
        return f"/uploads/{self.stored_filename}"

    model_config = {"from_attributes": True}


class DefectCaseCreate(BaseModel):
    production_date: dt.date
    detected_at: dt.datetime
    work_order_number: str = Field(min_length=1, max_length=60)
    drawer_part_reference: str | None = Field(default=None, max_length=120)
    found_station_id: int
    possible_source_station_id: int | None = None
    priority: str = "Normal"
    items: list[DefectItemIn] = Field(min_length=1)
    disposition: str | None = None
    # "Fixed immediately?" fast path (PROJECT_SPEC.md section 3.3) - New Defect
    # form toggle. True creates the case already closed (see defect_service.py
    # create_defect_case); only valid alongside disposition Rework/Scrap/Use As Is.
    resolved_on_the_spot: bool = False
    repair_action: str | None = None
    root_cause: str | None = None
    corrective_action: str | None = None
    notes: str | None = None

    @field_validator("work_order_number")
    @classmethod
    def _strip_wo(cls, v: str) -> str:
        return v.strip()


class DefectCaseUpdate(BaseModel):
    """Partial edit of a case. Adding new items merges into existing ones by category."""

    production_date: dt.date | None = None
    detected_at: dt.datetime | None = None
    work_order_number: str | None = Field(default=None, min_length=1, max_length=60)
    drawer_part_reference: str | None = None
    found_station_id: int | None = None
    possible_source_station_id: int | None = None
    priority: str | None = None
    repair_action: str | None = None
    root_cause: str | None = None
    corrective_action: str | None = None
    notes: str | None = None
    add_items: list[DefectItemIn] | None = None


class DefectCaseStatusChange(BaseModel):
    new_status: str
    disposition: str | None = None
    repair_action: str | None = None
    note: str | None = None


class DefectCaseOut(BaseModel):
    id: int
    case_number: str
    production_date: dt.date
    detected_at: dt.datetime
    work_order_number: str
    drawer_part_reference: str | None
    found_station_id: int
    found_station_name: str
    possible_source_station_id: int | None
    possible_source_station_name: str | None
    priority: str
    status: str
    disposition: str | None
    resolved_on_the_spot: bool
    skipped_recheck: bool
    repair_action: str | None
    root_cause: str | None
    corrective_action: str | None
    notes: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    closed_at: dt.datetime | None
    items: list[DefectItemOut]
    photos: list[DefectPhotoOut]
    status_history: list[StatusHistoryOut]
    defect_event_count: int
    is_deleted: bool

    @computed_field
    @property
    def detected_at_local(self) -> str | None:
        return to_display_string(self.detected_at)

    @computed_field
    @property
    def created_at_local(self) -> str | None:
        return to_display_string(self.created_at)

    @computed_field
    @property
    def closed_at_local(self) -> str | None:
        return to_display_string(self.closed_at)

    @computed_field
    @property
    def allowed_next_statuses(self) -> list[str]:
        """What the Rework Queue UI is allowed to offer next, straight from the one
        transition map in app/services/defect_service.py — the UI never re-implements
        this rule itself. Reopening a closed case to Open is always offered here too;
        the service layer still enforces that reopen requires a note."""
        options = sorted(allowed_next_statuses(self.status))
        if self.status in {"Closed - Repaired", "Closed - Scrapped", "Closed - Use As Is"}:
            options = ["Open", *options]
        return options

    @computed_field
    @property
    def direct_close_statuses(self) -> list[str]:
        """The 3 closed-status "close this case" targets (PROJECT_SPEC.md section
        3.3) - non-empty for every non-closed status, always requiring a note. Kept
        separate from allowed_next_statuses so the UI renders it as the primary
        close action rather than a normal status dropdown option."""
        return sorted(direct_close_statuses(self.status))

    model_config = {"from_attributes": True}


def defect_case_to_out(case) -> DefectCaseOut:
    """Build a DefectCaseOut from an ORM DefectCase, filling in derived/display fields."""
    return DefectCaseOut(
        id=case.id,
        case_number=case.case_number,
        production_date=case.production_date,
        detected_at=case.detected_at,
        work_order_number=case.work_order_number,
        drawer_part_reference=case.drawer_part_reference,
        found_station_id=case.found_station_id,
        found_station_name=case.found_station.name,
        possible_source_station_id=case.possible_source_station_id,
        possible_source_station_name=(
            case.possible_source_station.name if case.possible_source_station else None
        ),
        priority=case.priority,
        status=case.status,
        disposition=case.disposition,
        resolved_on_the_spot=case.resolved_on_the_spot,
        skipped_recheck=case.skipped_recheck,
        repair_action=case.repair_action,
        root_cause=case.root_cause,
        corrective_action=case.corrective_action,
        notes=case.notes,
        created_at=case.created_at,
        updated_at=case.updated_at,
        closed_at=case.closed_at,
        items=[
            DefectItemOut(
                id=i.id,
                defect_category_id=i.defect_category_id,
                defect_category_name=i.defect_category.name,
                affected_drawer_quantity=i.affected_drawer_quantity,
                notes=i.notes,
            )
            for i in case.items
        ],
        photos=list(case.photos),
        status_history=list(case.status_history),
        defect_event_count=sum(i.affected_drawer_quantity for i in case.items),
        is_deleted=case.is_deleted,
    )


class DefectCaseListOut(BaseModel):
    total: int
    cases: list[DefectCaseOut]


class WorkOrderLastStationOut(BaseModel):
    """New Defect form speed fix: what to pre-fill Found Station with when the
    operator re-types a work order that already has a case (see
    defect_service.get_last_case_for_work_order)."""

    work_order_number: str
    found_station_id: int
    found_station_name: str


# ---------------------------------------------------------------------------
# Daily production summary
# ---------------------------------------------------------------------------


class DailyProductionSummaryIn(BaseModel):
    shift: str = "Day"
    drawers_inspected: int = Field(ge=0)
    drawers_rejected_unique: int = Field(ge=0)
    drawers_reworked: int = Field(ge=0)
    # No longer a field on the Daily Summary form (scrap essentially doesn't happen
    # on this floor - see docs/PROJECT_SPEC_PHASE4.md "Scrap removal"). Optional and
    # defaulting to None (not 0) so upsert_daily_summary can tell "not provided by
    # this caller" apart from "explicitly zero" and preserve whatever a legacy row
    # already has instead of silently zeroing it. The MCP write tool and any direct
    # API caller can still pass an explicit value exactly as before.
    drawers_scrapped: int | None = Field(default=None, ge=0)
    notes: str | None = None


class DailySummarySuggestionOut(BaseModel):
    """Suggested "Unique Drawers Rejected"/"Drawers Reworked" values for the Daily
    Summary form, computed from real DefectCase data - see
    app/services/defect_service.py suggested_daily_counts()."""

    production_date: dt.date
    defect_case_count: int
    suggested_drawers_rejected_unique: int
    suggested_drawers_reworked: int


class DailyProductionSummaryOut(BaseModel):
    id: int
    production_date: dt.date
    shift: str
    drawers_inspected: int
    drawers_rejected_unique: int
    drawers_reworked: int
    drawers_scrapped: int
    notes: str | None
    cost_per_drawer_at_time: float | None
    warnings: list[str] = []

    @computed_field
    @property
    def internal_rework_cost(self) -> float | None:
        """Phase 4: null (not zero) when this row predates cost tracking and was
        never re-saved since - a real $0 rework cost is indistinguishable from
        "unknown rate" otherwise. See docs/PROJECT_SPEC_PHASE4.md."""
        if self.cost_per_drawer_at_time is None:
            return None
        return round(self.drawers_reworked * self.cost_per_drawer_at_time, 2)

    @computed_field
    @property
    def internal_scrap_cost(self) -> float | None:
        if self.cost_per_drawer_at_time is None:
            return None
        return round(self.drawers_scrapped * self.cost_per_drawer_at_time, 2)

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Reports / rework queue
# ---------------------------------------------------------------------------


class KpiOut(BaseModel):
    drawers_inspected: int
    defect_events: int
    unique_drawers_rejected: int
    drawers_reworked: int
    defects_per_100: float | None
    rejection_rate: float | None
    first_pass_yield: float | None
    rework_rate: float | None
    internal_rework_cost: float
    total_internal_quality_cost: float
    quality_cost_per_drawer_inspected: float | None
    # Phase 4 cost fix: which source(s) actually drove internal_rework_cost above -
    # "daily_summary", "defect_cases" (no summary existed for the period so cases
    # were the only source), "blended" (some dates had a summary, some didn't), or
    # "none" (no rework recorded either way). Scrap Rate / Internal Scrap Cost were
    # dropped from this app entirely - see docs/PROJECT_SPEC_PHASE4.md
    # "Scrap removal" - drawers_scrapped is no longer part of the KPI surface even
    # though the underlying DailyProductionSummary.drawers_scrapped column and the
    # Scrap disposition/status are both kept for backward compatibility.
    # See app/services/metrics_service.py:compute_internal_quality_cost.
    defect_case_rework_count: int
    cost_basis: str
    # PROJECT_SPEC.md section 3.3 KPIs (60-second-fix fast paths). total_cases /
    # queued_rework_count are the respective denominators, exposed for transparency
    # the same way defect_case_rework_count etc. are above.
    total_cases: int
    resolved_on_the_spot_count: int
    pct_resolved_on_the_spot: float | None
    queued_rework_count: int
    skipped_recheck_count: int
    pct_queued_rework_closed_without_recheck: float | None


class ParetoRowOut(BaseModel):
    label: str
    defect_events: int
    cumulative_pct: float | None


class TrendPointOut(BaseModel):
    period: str
    defect_events: int
    drawers_inspected: int
    unique_drawers_rejected: int
    internal_rework_cost: float = 0.0


class ReworkQueueItemOut(BaseModel):
    id: int
    case_number: str
    work_order_number: str
    categories: list[str]
    status: str
    disposition: str | None
    priority: str
    found_station_name: str
    possible_source_station_name: str | None
    detected_at: dt.datetime
    age_hours: float
    # Filled in later from the Rework Queue rather than at entry time (see
    # app/templates/defect_entry.html / rework_queue.html) - CLAUDE.md.
    root_cause: str | None
    corrective_action: str | None
    repair_action: str | None

    @computed_field
    @property
    def detected_at_local(self) -> str | None:
        return to_display_string(self.detected_at)

    @computed_field
    @property
    def allowed_next_statuses(self) -> list[str]:
        return sorted(allowed_next_statuses(self.status))

    @computed_field
    @property
    def direct_close_statuses(self) -> list[str]:
        """Drives the Rework Queue's primary "close this case" box - see
        DefectCaseOut.direct_close_statuses."""
        return sorted(direct_close_statuses(self.status))


class WorkOrderHistoryOut(BaseModel):
    work_order_number: str
    cases: list[DefectCaseOut]
    total_defect_events: int


# ---------------------------------------------------------------------------
# Customer issues (Phase 2) — kept separate from internal DefectCase schemas
# ---------------------------------------------------------------------------


class CustomerIssueCategoryOut(BaseModel):
    id: int
    name: str
    active: bool
    sort_order: int

    model_config = {"from_attributes": True}


class CustomerIssueCreate(BaseModel):
    reported_date: dt.date
    customer_name: str = Field(min_length=1, max_length=120)
    order_number: str | None = Field(default=None, max_length=60)
    issue_category_id: int
    source_type: str
    should_have_caught_at: str | None = Field(default=None, max_length=120)
    piece_count: int = Field(default=1, ge=1)
    estimated_rework_cost: float | None = None
    description: str = Field(min_length=1)
    photo_urls: str | None = None
    notes: str | None = None


class CustomerIssueUpdate(BaseModel):
    """Partial edit: resolve order number, change status, add notes, or link a case."""

    order_number: str | None = None
    status: str | None = None
    notes: str | None = None
    should_have_caught_at: str | None = None
    estimated_rework_cost: float | None = None
    piece_count: int | None = Field(default=None, ge=1)
    link_defect_case_id: int | None = None


class CustomerIssueOut(BaseModel):
    id: int
    issue_number: str
    reported_date: dt.date
    customer_name: str
    order_number: str | None
    issue_category_id: int
    issue_category_name: str
    source_type: str
    should_have_caught_at: str | None
    piece_count: int
    estimated_rework_cost: float | None
    description: str
    photo_urls: str | None
    status: str
    linked_defect_case_id: int | None
    linked_defect_case_number: str | None
    notes: str | None
    source_thread_id: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    is_deleted: bool

    @computed_field
    @property
    def order_number_missing(self) -> bool:
        """UI hook to highlight "order not identified" rows (PROJECT_SPEC_PHASE2.md)."""
        return not self.order_number

    @computed_field
    @property
    def is_synced(self) -> bool:
        """True if this row came from the production brief sync (PROJECT_SPEC_PHASE3.md),
        false if it was entered manually through the UI (phone/walk-in reports)."""
        return self.source_thread_id is not None

    model_config = {"from_attributes": True}


def customer_issue_to_out(issue) -> CustomerIssueOut:
    return CustomerIssueOut(
        id=issue.id,
        issue_number=issue.issue_number,
        reported_date=issue.reported_date,
        customer_name=issue.customer_name,
        order_number=issue.order_number,
        issue_category_id=issue.issue_category_id,
        issue_category_name=issue.issue_category.name,
        source_type=issue.source_type,
        should_have_caught_at=issue.should_have_caught_at,
        piece_count=issue.piece_count,
        estimated_rework_cost=(
            float(issue.estimated_rework_cost) if issue.estimated_rework_cost is not None else None
        ),
        description=issue.description,
        photo_urls=issue.photo_urls,
        status=issue.status,
        linked_defect_case_id=issue.linked_defect_case_id,
        linked_defect_case_number=(
            issue.linked_defect_case.case_number if issue.linked_defect_case else None
        ),
        notes=issue.notes,
        source_thread_id=issue.source_thread_id,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
        is_deleted=issue.is_deleted,
    )


class CustomerIssueListOut(BaseModel):
    total: int
    issues: list[CustomerIssueOut]


class CustomerIssueSummaryOut(BaseModel):
    total_issues: int
    total_pieces_affected: int
    total_estimated_cost: float
    escape_rate: float | None
    internal_catch_rate: float | None


class CustomerIssueParetoRowOut(BaseModel):
    label: str
    issue_count: int
    cumulative_pct: float | None


# ---------------------------------------------------------------------------
# Cost settings (Phase 4)
# ---------------------------------------------------------------------------


class CostSettingsOut(BaseModel):
    cost_per_drawer: float


class CostSettingsUpdate(BaseModel):
    cost_per_drawer: float = Field(gt=0)


# ---------------------------------------------------------------------------
# Production brief sync (Phase 3)
# ---------------------------------------------------------------------------


class SyncLogOut(BaseModel):
    id: int
    sync_started_at: dt.datetime
    sync_completed_at: dt.datetime | None
    source_url: str
    records_fetched: int
    records_created: int
    records_updated: int
    records_skipped: int
    errors: str | None
    status: str

    @computed_field
    @property
    def sync_started_at_local(self) -> str | None:
        return to_display_string(self.sync_started_at)

    @computed_field
    @property
    def sync_completed_at_local(self) -> str | None:
        return to_display_string(self.sync_completed_at)

    model_config = {"from_attributes": True}


class ManualSyncRequestOut(BaseModel):
    """Response to POST /api/v1/sync/customer-issues/request-manual-sync (the
    Customer Issues tab's "Sync Now" button) - always returned instantly, with no
    counts, since the actual fetch+ingest happens later on the local relay's next
    check-in."""

    message: str
    requested_at: dt.datetime

    @computed_field
    @property
    def requested_at_local(self) -> str | None:
        return to_display_string(self.requested_at)


class RelayHeartbeatOut(BaseModel):
    """Response to GET /api/v1/sync/customer-issues/relay-status - the local
    relay's frequent heartbeat check-in (see scripts/relay_poll.py)."""

    manual_sync_pending: bool


class RelayConnectionStatusOut(BaseModel):
    """Response to GET /api/v1/sync/customer-issues/relay-connection - drives the
    Customer Issues tab's 🟢/🔴 status line. relay_last_seen_at is null if the relay
    has never checked in."""

    relay_last_seen_at: dt.datetime | None
    relay_connected: bool
    manual_sync_pending: bool

    @computed_field
    @property
    def relay_last_seen_at_local(self) -> str | None:
        return to_display_string(self.relay_last_seen_at)


# ---------------------------------------------------------------------------
# Bulk actions (shared by DefectCase and CustomerIssue bulk-delete/bulk-restore)
# ---------------------------------------------------------------------------


class BulkIdsIn(BaseModel):
    ids: list[int] = Field(min_length=1)


class BulkActionOut(BaseModel):
    count: int
    ids: list[int]


# ---------------------------------------------------------------------------
# Generic API envelopes
# ---------------------------------------------------------------------------


class ErrorDetail(BaseModel):
    message: str
    field: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthOut(BaseModel):
    status: str
    database: str
