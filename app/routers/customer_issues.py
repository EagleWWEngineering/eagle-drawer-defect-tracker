"""Customer-reported issues (Phase 2). HTTP input/output only — business rules live
in app/services/customer_issue_service.py, mirroring the defect_cases.py pattern.

Route order matters here: literal paths ("/categories", "/summary", "/pareto") are
registered before the "/{issue_id}" parameterized route so they can never be shadowed.
"""

from __future__ import annotations

import datetime as dt
import decimal

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session, selectinload

from app.dependencies import get_actor_role, get_db
from app.errors import NotFoundError
from app.models import CustomerIssue, CustomerIssueCategory, DailyProductionSummary
from app.schemas import (
    CustomerIssueCategoryOut,
    CustomerIssueCreate,
    CustomerIssueListOut,
    CustomerIssueOut,
    CustomerIssueParetoRowOut,
    CustomerIssueSummaryOut,
    CustomerIssueUpdate,
    customer_issue_to_out,
)
from app.services import audit_service, customer_issue_service, export_service, metrics_service

router = APIRouter(prefix="/api/v1/customer-issues", tags=["customer-issues"])
export_router = APIRouter(prefix="/api/v1/exports", tags=["customer-issues"])


def _issue_query(db: Session):
    return db.query(CustomerIssue).options(
        selectinload(CustomerIssue.issue_category),
        selectinload(CustomerIssue.linked_defect_case),
    )


def _apply_filters(
    query,
    *,
    start_date: dt.date | None,
    end_date: dt.date | None,
    customer_name: str | None,
    order_number: str | None,
    category_id: int | None,
    source_type: str | None,
    should_have_caught_at: str | None,
    status: str | None,
    include_deleted: bool = False,
):
    if not include_deleted:
        query = query.filter(CustomerIssue.is_deleted.is_(False))
    if start_date is not None:
        query = query.filter(CustomerIssue.reported_date >= start_date)
    if end_date is not None:
        query = query.filter(CustomerIssue.reported_date <= end_date)
    if customer_name:
        query = query.filter(CustomerIssue.customer_name.ilike(f"%{customer_name}%"))
    if order_number:
        query = query.filter(CustomerIssue.order_number.ilike(f"%{order_number}%"))
    if category_id is not None:
        query = query.filter(CustomerIssue.issue_category_id == category_id)
    if source_type is not None:
        query = query.filter(CustomerIssue.source_type == source_type)
    if should_have_caught_at is not None:
        query = query.filter(CustomerIssue.should_have_caught_at == should_have_caught_at)
    if status is not None:
        query = query.filter(CustomerIssue.status == status)
    return query


@router.get("/categories", response_model=list[CustomerIssueCategoryOut])
def list_categories(db: Session = Depends(get_db)) -> list[CustomerIssueCategoryOut]:
    categories = (
        db.query(CustomerIssueCategory)
        .order_by(CustomerIssueCategory.sort_order, CustomerIssueCategory.name)
        .all()
    )
    return [CustomerIssueCategoryOut.model_validate(c) for c in categories]


@router.post("", response_model=CustomerIssueOut)
def create_issue(
    payload: CustomerIssueCreate,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> CustomerIssueOut:
    issue = customer_issue_service.create_customer_issue(
        db,
        reported_date=payload.reported_date,
        customer_name=payload.customer_name,
        order_number=payload.order_number,
        issue_category_id=payload.issue_category_id,
        source_type=payload.source_type,
        should_have_caught_at=payload.should_have_caught_at,
        piece_count=payload.piece_count,
        estimated_rework_cost=(
            decimal.Decimal(str(payload.estimated_rework_cost))
            if payload.estimated_rework_cost is not None
            else None
        ),
        description=payload.description,
        photo_urls=payload.photo_urls,
        notes=payload.notes,
    )
    audit_service.record(
        db,
        actor_role=actor_role,
        action="create",
        entity_type="CustomerIssue",
        entity_id=issue.issue_number,
        inputs=payload.model_dump(mode="json"),
        after={"issue_number": issue.issue_number, "status": issue.status},
    )
    return customer_issue_to_out(issue)


@router.get("", response_model=CustomerIssueListOut)
def list_issues(
    db: Session = Depends(get_db),
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    customer_name: str | None = None,
    order_number: str | None = None,
    category_id: int | None = None,
    source_type: str | None = None,
    should_have_caught_at: str | None = None,
    status: str | None = None,
    include_deleted: bool = False,
    page: int = 1,
    page_size: int = 50,
) -> CustomerIssueListOut:
    query = _apply_filters(
        _issue_query(db),
        start_date=start_date,
        end_date=end_date,
        customer_name=customer_name,
        order_number=order_number,
        category_id=category_id,
        source_type=source_type,
        should_have_caught_at=should_have_caught_at,
        status=status,
        include_deleted=include_deleted,
    )
    total = query.count()
    issues = (
        query.order_by(CustomerIssue.reported_date.desc(), CustomerIssue.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return CustomerIssueListOut(total=total, issues=[customer_issue_to_out(i) for i in issues])


@router.get("/summary", response_model=CustomerIssueSummaryOut)
def get_summary(
    db: Session = Depends(get_db),
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    customer_name: str | None = None,
    order_number: str | None = None,
    category_id: int | None = None,
    source_type: str | None = None,
    should_have_caught_at: str | None = None,
    status: str | None = None,
) -> CustomerIssueSummaryOut:
    issues = _apply_filters(
        _issue_query(db),
        start_date=start_date,
        end_date=end_date,
        customer_name=customer_name,
        order_number=order_number,
        category_id=category_id,
        source_type=source_type,
        should_have_caught_at=should_have_caught_at,
        status=status,
    ).all()
    totals = customer_issue_service.compute_summary(issues)

    drawers_inspected = 0
    if start_date is not None or end_date is not None:
        summary_query = db.query(DailyProductionSummary)
        if start_date is not None:
            summary_query = summary_query.filter(
                DailyProductionSummary.production_date >= start_date
            )
        if end_date is not None:
            summary_query = summary_query.filter(DailyProductionSummary.production_date <= end_date)
        drawers_inspected = sum(r.drawers_inspected for r in summary_query.all())

    internal_defect_events = 0
    if start_date is not None or end_date is not None:
        items_query = metrics_service.filtered_defect_items_query(
            db, start_date=start_date, end_date=end_date
        )
        internal_defect_events = sum(
            item.affected_drawer_quantity for item, _case in items_query.all()
        )

    rates = customer_issue_service.compute_escape_and_catch_rates(
        customer_issue_count=totals["total_issues"],
        drawers_inspected=drawers_inspected,
        internal_defect_events=internal_defect_events,
    )
    return CustomerIssueSummaryOut(**totals, **rates)


@router.get("/pareto", response_model=list[CustomerIssueParetoRowOut])
def get_pareto(
    db: Session = Depends(get_db),
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    group_by: str = "category",
    limit: int = 10,
) -> list[CustomerIssueParetoRowOut]:
    """group_by: 'category' (default) or 'should_have_caught_at' — the latter shows
    where Eagle's internal QC is failing to catch problems before shipment."""
    issues = _apply_filters(
        _issue_query(db),
        start_date=start_date,
        end_date=end_date,
        customer_name=None,
        order_number=None,
        category_id=None,
        source_type=None,
        should_have_caught_at=None,
        status=None,
    ).all()

    counts: dict[str, int] = {}
    for issue in issues:
        if group_by == "should_have_caught_at":
            label = issue.should_have_caught_at or "Unknown"
        else:
            label = issue.issue_category.name
        counts[label] = counts.get(label, 0) + 1

    rows = metrics_service.compute_pareto(counts)
    return [
        CustomerIssueParetoRowOut(
            label=r["label"], issue_count=r["defect_events"], cumulative_pct=r["cumulative_pct"]
        )
        for r in rows[:limit]
    ]


@router.get("/{issue_id}", response_model=CustomerIssueOut)
def get_issue(issue_id: int, db: Session = Depends(get_db)) -> CustomerIssueOut:
    issue = _issue_query(db).filter(CustomerIssue.id == issue_id).first()
    if issue is None or issue.is_deleted:
        raise NotFoundError(f"Customer issue {issue_id} not found.")
    return customer_issue_to_out(issue)


@router.patch("/{issue_id}", response_model=CustomerIssueOut)
def update_issue(
    issue_id: int,
    payload: CustomerIssueUpdate,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> CustomerIssueOut:
    issue = customer_issue_service.get_issue_or_404(db, issue_id)
    before = customer_issue_to_out(issue).model_dump(mode="json")

    if payload.link_defect_case_id is not None:
        customer_issue_service.link_to_defect_case(db, issue, payload.link_defect_case_id)

    update_kwargs: dict = {}
    data = payload.model_dump(exclude_unset=True, exclude={"link_defect_case_id"})
    if "order_number" in data:
        update_kwargs["order_number"] = data["order_number"]
    if "status" in data:
        update_kwargs["status"] = data["status"]
    if "notes" in data:
        update_kwargs["notes"] = data["notes"]
    if "should_have_caught_at" in data:
        update_kwargs["should_have_caught_at"] = data["should_have_caught_at"]
    if "piece_count" in data:
        update_kwargs["piece_count"] = data["piece_count"]
    if "estimated_rework_cost" in data:
        value = data["estimated_rework_cost"]
        update_kwargs["estimated_rework_cost"] = (
            decimal.Decimal(str(value)) if value is not None else None
        )

    if update_kwargs:
        customer_issue_service.update_customer_issue(db, issue, **update_kwargs)

    db.refresh(issue)
    audit_service.record(
        db,
        actor_role=actor_role,
        action="update",
        entity_type="CustomerIssue",
        entity_id=issue.issue_number,
        inputs=payload.model_dump(exclude_unset=True, mode="json"),
        before=before,
        after=customer_issue_to_out(issue).model_dump(mode="json"),
    )
    return customer_issue_to_out(issue)


@router.delete("/{issue_id}", response_model=CustomerIssueOut)
def soft_delete_issue(
    issue_id: int,
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
) -> CustomerIssueOut:
    issue = customer_issue_service.get_issue_or_404(db, issue_id)
    deleted = customer_issue_service.soft_delete_issue(db, issue)
    audit_service.record(
        db,
        actor_role=actor_role,
        action="soft_delete",
        entity_type="CustomerIssue",
        entity_id=issue.issue_number,
    )
    return customer_issue_to_out(deleted)


@export_router.get("/customer-issues.csv")
def export_customer_issues_csv(
    db: Session = Depends(get_db),
    actor_role: str = Depends(get_actor_role),
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    customer_name: str | None = None,
    order_number: str | None = None,
    category_id: int | None = None,
    source_type: str | None = None,
    should_have_caught_at: str | None = None,
    status: str | None = None,
) -> Response:
    issues = _apply_filters(
        _issue_query(db),
        start_date=start_date,
        end_date=end_date,
        customer_name=customer_name,
        order_number=order_number,
        category_id=category_id,
        source_type=source_type,
        should_have_caught_at=should_have_caught_at,
        status=status,
    ).all()
    csv_text = export_service.build_customer_issues_csv(issues)

    audit_service.record(
        db,
        actor_role=actor_role,
        action="export",
        entity_type="CustomerIssue",
        entity_id=None,
        inputs={
            "start_date": str(start_date) if start_date else None,
            "end_date": str(end_date) if end_date else None,
            "customer_name": customer_name,
            "order_number": order_number,
            "category_id": category_id,
            "source_type": source_type,
            "should_have_caught_at": should_have_caught_at,
            "status": status,
        },
        message=f"{len(issues)} rows exported",
    )

    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=customer_issues.csv"},
    )
