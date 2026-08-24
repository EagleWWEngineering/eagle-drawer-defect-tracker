"""Pydantic v2 request/response models. Validation shape only — business rules live
in app/services/*.py so the same rules apply no matter which router calls them.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field, computed_field, field_validator

from app.services.defect_service import (
    CLOSED_STATUSES,
    allowed_next_statuses,
    direct_close_statuses,
)
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
    # Write-legal values only (PROJECT_SPEC_PHASE7.md) - what a case can be
    # CREATED or CHANGED to. Use all_statuses/all_dispositions below for a
    # filter/report dropdown that also needs to reach retired historical values.
    statuses: list[str]
    dispositions: list[str]
    # Every historically-possible value, retired ones included - for filter
    # dropdowns (Reports/Dashboard) only, so a historical case in a retired
    # status/disposition can still be filtered to. Never used for write validation.
    all_statuses: list[str]
    all_dispositions: list[str]


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
    # "Fixed immediately?" fast path (PROJECT_SPEC_PHASE7.md) - New Defect form
    # toggle. True creates the case already closed (see defect_service.py
    # create_defect_case); only valid alongside disposition "Rework" now (Set
    # Aside means "waiting to be worked", the opposite of "already done").
    resolved_on_the_spot: bool = False
    # Which closed status resolved_on_the_spot lands the case in - "Repaired"
    # (default) or "Use As Is". Only meaningful alongside resolved_on_the_spot;
    # see defect_service.py INSTANT_CLOSE_OUTCOMES / create_defect_case.
    instant_close_outcome: str | None = None
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
    # Phase 7 cost model (PROJECT_SPEC_PHASE7.md): the cost_per_drawer rate
    # snapshotted at creation. Null for a case created before this column existed
    # - see app/services/metrics_service.py compute_case_cost for the read-time
    # fallback used when computing actual cost figures.
    cost_per_drawer_at_time: float | None
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
        if self.status in CLOSED_STATUSES:
            options = ["Open", *options]
        return options

    @computed_field
    @property
    def direct_close_statuses(self) -> list[str]:
        """The 2 closed-status "close this case" targets (PROJECT_SPEC_PHASE7.md)
        - non-empty for every non-closed status, with an optional note. Kept
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
        cost_per_drawer_at_time=(
            float(case.cost_per_drawer_at_time)
            if case.cost_per_drawer_at_time is not None
            else None
        ),
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
    # No longer a field on the Daily Summary form (PROJECT_SPEC_PHASE7.md: Rework
    # Rate is now derived from defect cases, not a hand-entered count - a second
    # number for the same fact was a contradiction waiting to happen). Optional and
    # defaulting to None (not 0), same pattern as drawers_scrapped below, so
    # upsert_daily_summary preserves whatever a legacy row already has instead of
    # silently zeroing it. The MCP write tool and any direct API caller can still
    # pass an explicit value.
    drawers_reworked: int | None = Field(default=None, ge=0)
    # No longer a field on the Daily Summary form (scrap essentially doesn't happen
    # on this floor - see docs/PROJECT_SPEC_PHASE4.md "Scrap removal"). Optional and
    # defaulting to None (not 0) so upsert_daily_summary can tell "not provided by
    # this caller" apart from "explicitly zero" and preserve whatever a legacy row
    # already has instead of silently zeroing it. The MCP write tool and any direct
    # API caller can still pass an explicit value exactly as before.
    drawers_scrapped: int | None = Field(default=None, ge=0)
    notes: str | None = None


class DailySummarySuggestionOut(BaseModel):
    """Suggested "Unique Drawers Rejected" value for the Daily Summary form,
    computed from real DefectCase data - see
    app/services/defect_service.py suggested_daily_counts(). No longer suggests a
    reworked count (PROJECT_SPEC_PHASE7.md - that field left the form; Rework Rate
    is computed straight from cases, not a suggested/typed number)."""

    production_date: dt.date
    defect_case_count: int
    suggested_drawers_rejected_unique: int


class DailyProductionSummaryOut(BaseModel):
    id: int
    production_date: dt.date
    shift: str
    drawers_inspected: int
    drawers_rejected_unique: int
    drawers_reworked: int
    drawers_scrapped: int
    notes: str | None
    # Historical rate snapshot - kept for the record, but PROJECT_SPEC_PHASE7.md's
    # cost model no longer derives any cost figure from this row (drawers_reworked/
    # drawers_scrapped * this rate). Internal Quality Cost is entirely case-derived
    # now - see app/services/metrics_service.py compute_internal_quality_cost. The
    # internal_rework_cost/internal_scrap_cost computed fields that used to live
    # here were removed for exactly that reason: they'd otherwise show a real
    # dollar figure that has nothing to do with the actual reported cost anymore.
    cost_per_drawer_at_time: float | None
    warnings: list[str] = []
    # Read-only, not stored on this row and not part of a save payload - set
    # explicitly by the router after model_validate() from
    # defect_service.count_rework_cases_by_date(), same pattern `warnings`
    # above already uses. PROJECT_SPEC_PHASE7.md: Rework Rate's numerator
    # (cases with disposition "Rework" for this production_date), surfaced for
    # reference on the Daily Summary page now that there's no editable
    # drawers_reworked field to show it next to.
    reworked_case_count: int = 0

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Daily schedule (Phase 6) — see app/services/schedule_service.py
# ---------------------------------------------------------------------------


class DailyScheduleOut(BaseModel):
    production_date: dt.date
    drawers_scheduled: int
    source: str
    synced_at: dt.datetime | None
    updated_at: dt.datetime

    @computed_field
    @property
    def is_synced(self) -> bool:
        """True = "from production brief", False = "entered manually" - the
        Daily Summary form's synced-vs-manual indicator (see
        docs/PROJECT_SPEC.md Phase 6 addendum)."""
        return self.source == "sync"

    @computed_field
    @property
    def synced_at_local(self) -> str | None:
        return to_display_string(self.synced_at)

    model_config = {"from_attributes": True}


class DailyScheduleIn(BaseModel):
    """PUT /api/v1/daily-production/schedule body - manual entry/override. Always
    sets source="manual" (see schedule_service.upsert_schedule) and pins the date
    against future sync overwrites."""

    production_date: dt.date
    drawers_scheduled: int = Field(ge=0)


class DailyScheduleListOut(BaseModel):
    schedules: list[DailyScheduleOut]


class ScheduleAttainmentDayOut(BaseModel):
    """One bar-pair on the Dashboard's Scheduled vs Completed chart.
    drawers_scheduled is None (renders as "—") when no daily_schedules row exists
    for that date - never 0, which would mean "the brief scheduled zero drawers"."""

    production_date: dt.date
    drawers_scheduled: int | None
    drawers_inspected: int


class ScheduleAttainmentOut(BaseModel):
    """GET /api/v1/daily-production/schedule-attainment - see
    app/services/metrics_service.py build_schedule_vs_completed()."""

    days: list[ScheduleAttainmentDayOut]
    total_scheduled: int | None
    total_inspected: int
    attainment_pct: float | None


class DatePresetOut(BaseModel):
    """GET /api/v1/reports/date-preset - see
    app/timezone_utils.py resolve_date_preset()."""

    start_date: dt.date
    end_date: dt.date


# ---------------------------------------------------------------------------
# Reports / rework queue
# ---------------------------------------------------------------------------


class KpiOut(BaseModel):
    drawers_inspected: int
    defect_events: int
    unique_drawers_rejected: int
    # Phase 7 (PROJECT_SPEC_PHASE7.md): redefined - count of distinct cases with
    # disposition "Rework" in the filtered range (Rework Rate's numerator), not a
    # sum of the now-removed Daily Production Summary "Drawers reworked" field.
    drawers_reworked: int
    defects_per_100: float | None
    rejection_rate: float | None
    first_pass_yield: float | None
    rework_rate: float | None
    # Phase 7 cost model: sum of one cost unit per non-"Closed - Use As Is" case in
    # the filtered range. Scrap Rate / Internal Scrap Cost stay dropped from this
    # app entirely (docs/PROJECT_SPEC_PHASE4.md "Scrap removal"). See
    # app/services/metrics_service.py compute_internal_quality_cost.
    internal_rework_cost: float
    # Phase 7, new: the cost that would have been incurred by every case in the
    # filtered range that instead closed "Closed - Use As Is" - what "shipping as
    # is instead of reworking" saved.
    cost_avoided: float
    total_internal_quality_cost: float
    quality_cost_per_drawer_inspected: float | None
    # PROJECT_SPEC.md section 3.3 KPI (60-second-fix fast path). total_cases is its
    # denominator, exposed for transparency the same way other counts here are.
    total_cases: int
    resolved_on_the_spot_count: int
    pct_resolved_on_the_spot: float | None


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
    # Phase 7, new: see KpiOut.cost_avoided - same figure, bucketed the same way as
    # every other field on this response.
    cost_avoided: float = 0.0
    # Phase 6: None (not 0) when no daily_schedules row falls in this bucket at
    # all - see app/services/metrics_service.py build_schedule_vs_completed's
    # docstring for why a real "scheduled zero" must stay distinguishable.
    drawers_scheduled: int | None = None
    schedule_attainment_pct: float | None = None


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
