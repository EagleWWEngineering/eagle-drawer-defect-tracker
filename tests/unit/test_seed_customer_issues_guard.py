"""Verifies scripts/seed_customer_issues.py skips seeding once real synced data
(source_thread_id is not null) is present, so demo data never mixes with real
production-brief data (Phase 3)."""

from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path

import pytest

from app.models import CustomerIssue

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

seed_customer_issues = importlib.import_module("scripts.seed_customer_issues")


@pytest.fixture()
def _patched_session(monkeypatch, db_session):
    """The script imports SessionLocal at module scope and calls SessionLocal()
    itself; patch it to a factory that always returns this test's shared session
    (already seeded with master data) so the script never touches the real DB."""
    monkeypatch.setattr(seed_customer_issues, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)  # keep it open between calls
    return db_session


def test_seeds_when_no_synced_data_present(_patched_session, customer_categories):
    seed_customer_issues.seed_customer_issues(count=5, days=5, seed=1)
    count = _patched_session.query(CustomerIssue).count()
    assert count == 5


def test_skips_when_synced_data_already_present(_patched_session, customer_categories):
    from app.services import customer_issue_service

    customer_issue_service.create_customer_issue(
        _patched_session,
        reported_date=dt.date(2026, 7, 24),
        customer_name="Real Customer",
        order_number=None,
        issue_category_id=customer_categories["Other"].id,
        source_type="Manufacturing",
        should_have_caught_at=None,
        piece_count=1,
        estimated_rework_cost=None,
        description="Real synced issue",
        photo_urls=None,
        notes=None,
    )
    real_issue = _patched_session.query(CustomerIssue).first()
    real_issue.source_thread_id = "real-thread-123"
    _patched_session.commit()

    seed_customer_issues.seed_customer_issues(count=5, days=5, seed=1)

    count = _patched_session.query(CustomerIssue).count()
    assert count == 1  # unchanged - the demo seed must have skipped itself
