"""Customer-reported issue business rules (Phase 2).

Deliberately kept separate from app/services/defect_service.py: a customer issue is
a different kind of record (a replicated daily-production-brief row, not an internal
QC finding) with its own numbering, validation, and KPI formulas. The only place the
two connect is CustomerIssue.linked_defect_case_id, set via link_to_defect_case().

Nothing here changes DefectCase/DefectItem counting rules from PROJECT_SPEC.md.
"""

from __future__ import annotations

import datetime as dt
import decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.errors import NotFoundError, ValidationError
from app.models import CustomerIssue, CustomerIssueCategory, DefectCase
from app.services.metrics_service import round_rate

VALID_SOURCE_TYPES: list[str] = ["Manufacturing", "Shipping Damage"]
VALID_STATUSES: list[str] = ["Open", "Ignored", "Linked"]

BASE_REWORK_COST_PER_PIECE = decimal.Decimal("100.00")


def _get_category(db: Session, category_id: int) -> CustomerIssueCategory:
    category = db.get(CustomerIssueCategory, category_id)
    if category is None:
        raise NotFoundError(
            f"Customer issue category {category_id} does not exist.", field="issue_category_id"
        )
    return category


def generate_issue_number(db: Session, reported_date: dt.date) -> str:
    """CI-YYYYMMDD-NNNN with a 4-digit sequence that resets per reported_date.

    Mirrors generate_case_number() in defect_service.py for consistency, but counts
    CustomerIssue rows instead of DefectCase rows.
    """
    count = (
        db.query(func.count(CustomerIssue.id))
        .filter(CustomerIssue.reported_date == reported_date)
        .scalar()
        or 0
    )
    sequence = count + 1
    return f"CI-{reported_date.strftime('%Y%m%d')}-{sequence:04d}"


def _default_rework_cost(piece_count: int) -> decimal.Decimal:
    return BASE_REWORK_COST_PER_PIECE * piece_count


def create_customer_issue(
    db: Session,
    *,
    reported_date: dt.date,
    customer_name: str,
    order_number: str | None,
    issue_category_id: int,
    source_type: str,
    should_have_caught_at: str | None,
    piece_count: int,
    estimated_rework_cost: decimal.Decimal | None,
    description: str,
    photo_urls: str | None,
    notes: str | None,
) -> CustomerIssue:
    if not customer_name or not customer_name.strip():
        raise ValidationError("Customer name is required.", field="customer_name")
    if source_type not in VALID_SOURCE_TYPES:
        raise ValidationError(
            f"Source type must be one of {VALID_SOURCE_TYPES}.", field="source_type"
        )
    if not description or not description.strip():
        raise ValidationError("Description is required.", field="description")
    if piece_count < 1:
        raise ValidationError("Piece count must be at least 1.", field="piece_count")

    _get_category(db, issue_category_id)

    issue = CustomerIssue(
        issue_number=generate_issue_number(db, reported_date),
        reported_date=reported_date,
        customer_name=customer_name.strip(),
        order_number=(order_number or None),
        issue_category_id=issue_category_id,
        source_type=source_type,
        should_have_caught_at=(should_have_caught_at or None),
        piece_count=piece_count,
        estimated_rework_cost=(
            estimated_rework_cost
            if estimated_rework_cost is not None
            else _default_rework_cost(piece_count)
        ),
        description=description.strip(),
        photo_urls=(photo_urls or None),
        status="Open",
        notes=notes,
    )
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return issue


def get_issue_or_404(db: Session, issue_id: int) -> CustomerIssue:
    issue = db.get(CustomerIssue, issue_id)
    if issue is None or issue.is_deleted:
        raise NotFoundError(f"Customer issue {issue_id} not found.")
    return issue


def update_customer_issue(
    db: Session,
    issue: CustomerIssue,
    *,
    order_number: str | None = ...,
    status: str | None = None,
    notes: str | None = ...,
    should_have_caught_at: str | None = ...,
    estimated_rework_cost: decimal.Decimal | None = ...,
    piece_count: int | None = None,
) -> CustomerIssue:
    """Partial edit: resolve order number, change status, add notes, correct fields.

    Uses `...` (Ellipsis) as the "not provided" sentinel for nullable fields so a
    caller can still explicitly set them back to None (e.g. clearing notes),
    matching the DefectCaseUpdate pattern in defect_service.py.
    """
    if status is not None:
        if status not in VALID_STATUSES:
            raise ValidationError(f"Status must be one of {VALID_STATUSES}.", field="status")
        issue.status = status
    if order_number is not ...:
        issue.order_number = order_number
    if notes is not ...:
        issue.notes = notes
    if should_have_caught_at is not ...:
        issue.should_have_caught_at = should_have_caught_at
    if piece_count is not None:
        if piece_count < 1:
            raise ValidationError("Piece count must be at least 1.", field="piece_count")
        issue.piece_count = piece_count
    if estimated_rework_cost is not ...:
        issue.estimated_rework_cost = estimated_rework_cost

    db.commit()
    db.refresh(issue)
    return issue


def link_to_defect_case(db: Session, issue: CustomerIssue, defect_case_id: int) -> CustomerIssue:
    """Link a customer issue to an internal defect case and mark it Linked."""
    case = db.get(DefectCase, defect_case_id)
    if case is None or case.is_deleted:
        raise NotFoundError(
            f"Defect case {defect_case_id} not found.", field="linked_defect_case_id"
        )
    issue.linked_defect_case_id = defect_case_id
    issue.status = "Linked"
    db.commit()
    db.refresh(issue)
    return issue


def ignore_issue(db: Session, issue: CustomerIssue) -> CustomerIssue:
    issue.status = "Ignored"
    db.commit()
    db.refresh(issue)
    return issue


def soft_delete_issue(db: Session, issue: CustomerIssue) -> CustomerIssue:
    issue.is_deleted = True
    db.commit()
    db.refresh(issue)
    return issue


def bulk_soft_delete_issues(db: Session, ids: list[int]) -> list[CustomerIssue]:
    """Soft-delete every not-already-deleted issue in `ids`. IDs that don't exist
    or are already deleted are silently skipped - the caller only gets back the
    issues it actually changed."""
    issues = (
        db.query(CustomerIssue)
        .filter(CustomerIssue.id.in_(ids), CustomerIssue.is_deleted.is_(False))
        .all()
    )
    for issue in issues:
        issue.is_deleted = True
    db.commit()
    for issue in issues:
        db.refresh(issue)
    return issues


def bulk_restore_issues(db: Session, ids: list[int]) -> list[CustomerIssue]:
    """Restore every currently-deleted issue in `ids`. Same skip-silently rule as
    bulk_soft_delete_issues for IDs that don't exist or aren't deleted."""
    issues = (
        db.query(CustomerIssue)
        .filter(CustomerIssue.id.in_(ids), CustomerIssue.is_deleted.is_(True))
        .all()
    )
    for issue in issues:
        issue.is_deleted = False
    db.commit()
    for issue in issues:
        db.refresh(issue)
    return issues


def compute_summary(issues: list[CustomerIssue]) -> dict:
    """Total issues, total pieces, total estimated cost for a (pre-filtered) set."""
    total_issues = len(issues)
    total_pieces = sum(i.piece_count for i in issues)
    total_cost = sum((i.estimated_rework_cost or decimal.Decimal("0")) for i in issues)
    return {
        "total_issues": total_issues,
        "total_pieces_affected": total_pieces,
        "total_estimated_cost": float(total_cost),
    }


def compute_escape_and_catch_rates(
    *, customer_issue_count: int, drawers_inspected: int, internal_defect_events: int
) -> dict:
    """PROJECT_SPEC_PHASE2.md KPI formulas. Never divides by zero.

    Escape Rate = (Customer Issues / Drawers Inspected) * 100
    Internal Catch Rate = Internal Defect Events / (Internal Defect Events + Customer
    Issues) * 100
    """
    escape_rate = (
        None if drawers_inspected == 0 else (customer_issue_count / drawers_inspected) * 100
    )
    denominator = internal_defect_events + customer_issue_count
    internal_catch_rate = None if denominator == 0 else (internal_defect_events / denominator) * 100
    return {
        "escape_rate": round_rate(escape_rate),
        "internal_catch_rate": round_rate(internal_catch_rate),
    }
