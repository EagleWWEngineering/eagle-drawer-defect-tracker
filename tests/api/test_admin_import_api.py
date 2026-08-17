"""End-to-end test of the TEMPORARY one-time migration endpoint
(POST /api/v1/admin/import-data, app/routers/admin.py) and its underlying
natural-key remapping logic (app/services/migration_service.py).

The "source" database below is deliberately seeded so its Station/DefectCategory/
CustomerIssueCategory rows get different integer ids than the "target" database's
(same names, offset ids) - the exact real-world scenario the whole natural-key
design exists to handle. Every assertion about a remapped foreign key compares
against the TARGET database's own id for that name, and also asserts it differs
from the SOURCE's raw id, so this test would fail if the import ever regressed to
naively copying ids across databases instead of resolving them by name.
"""

from __future__ import annotations

import datetime as dt

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
from app.database import Base
from app.dependencies import get_db
from app.main import app
from app.models import (
    CustomerIssue,
    CustomerIssueCategory,
    DailyProductionSummary,
    DefectCase,
    DefectCategory,
    DefectPhoto,
    Station,
)
from app.seed_data import seed_master_data
from app.services import auth_service, customer_issue_service, defect_service, migration_service

TEST_USERNAME = "importtester"
TEST_PASSWORD = "s3cret-import-password"
PHOTO_BYTES = b"\x89PNG\r\n\x1a\nfake-but-real-bytes-for-a-test-photo"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _new_engine_and_sessionmaker():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def source_db():
    """A standalone DB, never wired to the FastAPI app, standing in for
    Rodolfo's local dev SQLite database. Seeded with one throwaway row per
    master-data table BEFORE seed_master_data() runs, so every real station/
    category name ends up at a different id than in a normally-seeded
    "target" database (see target_client below) - same names, offset ids."""
    _engine, SourceSession = _new_engine_and_sessionmaker()
    db = SourceSession()
    db.add(Station(name="ZZZ Throwaway Station", active=True, sort_order=999))
    db.add(DefectCategory(name="ZZZ Throwaway Category", active=True, sort_order=999))
    db.add(
        CustomerIssueCategory(name="ZZZ Throwaway Customer Category", active=True, sort_order=999)
    )
    db.commit()
    seed_master_data(db)
    yield db
    db.close()


@pytest.fixture()
def source_uploads_dir(tmp_path):
    d = tmp_path / "source-uploads"
    d.mkdir()
    return d


@pytest.fixture()
def target_client(monkeypatch, tmp_path):
    """A full FastAPI TestClient wired to its own, normally-seeded ("target")
    database - standing in for the live Render instance. Sets the shared
    credential via env vars *before* seeding, since app startup
    (seed_master_data -> sync_credentials_from_env) only ever reads the
    environment at seed time, matching the existing raw_client pattern in
    tests/api/test_auth_api.py."""
    monkeypatch.setenv("APP_USERNAME", TEST_USERNAME)
    monkeypatch.setenv(
        "APP_PASSWORD_HASH",
        bcrypt.hashpw(TEST_PASSWORD.encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
    )

    _engine, TestingSession = _new_engine_and_sessionmaker()
    seed_session = TestingSession()
    seed_master_data(seed_session)
    seed_session.close()

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    settings = config.get_settings()
    monkeypatch.setattr(settings, "uploads_dir", tmp_path / "target-uploads")

    test_client = TestClient(app)
    test_client.testing_sessionmaker = TestingSession

    auth_db = TestingSession()
    token = auth_service.create_session(auth_db)
    auth_db.close()
    test_client.cookies.set(auth_service.SESSION_COOKIE_NAME, token)

    try:
        yield test_client
    finally:
        test_client.close()
        app.dependency_overrides.clear()


def _build_source_data(source_db, source_uploads_dir):
    """Populate the source DB with one of everything, including a CustomerIssue
    linked to a DefectCase and a DefectPhoto backed by a real (small, synthetic)
    file on disk."""
    stations = {s.name: s for s in source_db.query(Station).all()}
    categories = {c.name: c for c in source_db.query(DefectCategory).all()}
    customer_categories = {c.name: c for c in source_db.query(CustomerIssueCategory).all()}

    case = defect_service.create_defect_case(
        source_db,
        production_date=dt.date(2026, 7, 1),
        detected_at=dt.datetime(2026, 7, 1, 9, 0, tzinfo=dt.timezone.utc),
        work_order_number="WO-REAL-001",
        drawer_part_reference="Left side panel",
        found_station_id=stations["Dado"].id,
        possible_source_station_id=stations["Assembly"].id,
        priority="High",
        items=[
            {
                "defect_category_id": categories["Dado / Bottom Groove"].id,
                "affected_drawer_quantity": 2,
                "notes": "Groove too shallow",
            }
        ],
        disposition="Rework",
    )
    defect_service.update_case_status(
        source_db,
        case,
        new_status="Closed - Repaired",
        disposition="Rework",
        repair_action="Re-cut groove to spec",
        note="Fixed on rework pass",
    )

    stored_filename = f"{case.case_number}_test.png"
    (source_uploads_dir / stored_filename).write_bytes(PHOTO_BYTES)
    source_db.add(
        DefectPhoto(
            defect_case_id=case.id,
            stored_filename=stored_filename,
            original_filename="drawer.png",
            content_type="image/png",
            uploaded_at=dt.datetime(2026, 7, 1, 9, 5, tzinfo=dt.timezone.utc),
        )
    )
    source_db.commit()

    issue = customer_issue_service.create_customer_issue(
        source_db,
        reported_date=dt.date(2026, 7, 2),
        customer_name="Jane Doe",
        order_number="ORD-555",
        issue_category_id=customer_categories["Finish Quality"].id,
        source_type="Manufacturing",
        should_have_caught_at="Top Coat",
        piece_count=1,
        estimated_rework_cost=None,
        description="Finish looked off on delivery.",
        photo_urls=None,
        notes=None,
    )
    customer_issue_service.link_to_defect_case(source_db, issue, case.id)

    defect_service.upsert_daily_summary(
        source_db,
        production_date=dt.date(2026, 7, 1),
        shift="Day",
        drawers_inspected=50,
        drawers_rejected_unique=1,
        drawers_reworked=1,
        drawers_scrapped=0,
        notes=None,
    )

    source_db.refresh(case)
    source_db.refresh(issue)
    return case, issue


# ---------------------------------------------------------------------------
# Happy path: export from source, import into target, verify remapping
# ---------------------------------------------------------------------------


def test_import_creates_records_with_ids_correctly_remapped_by_name(
    source_db, source_uploads_dir, target_client
):
    case, issue = _build_source_data(source_db, source_uploads_dir)
    bundle = migration_service.export_bundle(source_db, source_uploads_dir)

    resp = target_client.post(
        "/api/v1/admin/import-data", json={**bundle, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["defect_cases"] == {"created": 1, "updated": 0, "skipped": 0, "errors": []}
    assert body["customer_issues"] == {"created": 1, "updated": 0, "skipped": 0, "errors": []}
    assert body["daily_production_summaries"] == {
        "created": 1,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }

    target_db = target_client.testing_sessionmaker()
    try:
        target_case = (
            target_db.query(DefectCase).filter(DefectCase.case_number == case.case_number).one()
        )
        target_dado = target_db.query(Station).filter(Station.name == "Dado").one()
        target_assembly = target_db.query(Station).filter(Station.name == "Assembly").one()

        # Correctly resolved to the TARGET's own ids...
        assert target_case.found_station_id == target_dado.id
        assert target_case.possible_source_station_id == target_assembly.id
        # ...which are NOT the same as the raw ids carried on the source row -
        # proving this went through name resolution, not a raw-id copy.
        assert target_case.found_station_id != case.found_station_id
        assert target_case.possible_source_station_id != case.possible_source_station_id

        assert len(target_case.items) == 1
        target_category = (
            target_db.query(DefectCategory)
            .filter(DefectCategory.name == "Dado / Bottom Groove")
            .one()
        )
        assert target_case.items[0].defect_category_id == target_category.id
        assert target_case.items[0].defect_category_id != case.items[0].defect_category_id
        assert target_case.items[0].affected_drawer_quantity == 2

        assert len(target_case.status_history) == 2  # "Case created" + the update
        assert len(target_case.photos) == 1
        target_photo = target_case.photos[0]
        target_settings = config.get_settings()
        on_disk = (target_settings.uploads_dir / target_photo.stored_filename).read_bytes()
        assert on_disk == PHOTO_BYTES

        target_issue = (
            target_db.query(CustomerIssue)
            .filter(CustomerIssue.issue_number == issue.issue_number)
            .one()
        )
        target_finish_quality = (
            target_db.query(CustomerIssueCategory)
            .filter(CustomerIssueCategory.name == "Finish Quality")
            .one()
        )
        assert target_issue.issue_category_id == target_finish_quality.id
        assert target_issue.issue_category_id != issue.issue_category_id
        # The whole point of exporting by case_number instead of raw id: this
        # must point at the TARGET's own case row, not the source's raw id.
        assert target_issue.linked_defect_case_id == target_case.id

        summary = (
            target_db.query(DailyProductionSummary)
            .filter(
                DailyProductionSummary.production_date == dt.date(2026, 7, 1),
                DailyProductionSummary.shift == "Day",
            )
            .one()
        )
        assert summary.drawers_inspected == 50
        assert summary.drawers_reworked == 1
    finally:
        target_db.close()


def test_import_is_idempotent_on_rerun(source_db, source_uploads_dir, target_client):
    case, _issue = _build_source_data(source_db, source_uploads_dir)
    bundle = migration_service.export_bundle(source_db, source_uploads_dir)

    first = target_client.post(
        "/api/v1/admin/import-data", json={**bundle, "password": TEST_PASSWORD}
    )
    assert first.status_code == 200

    second = target_client.post(
        "/api/v1/admin/import-data", json={**bundle, "password": TEST_PASSWORD}
    )
    assert second.status_code == 200
    body = second.json()
    assert body["defect_cases"] == {"created": 0, "updated": 1, "skipped": 0, "errors": []}
    assert body["customer_issues"] == {"created": 0, "updated": 1, "skipped": 0, "errors": []}
    assert body["daily_production_summaries"] == {
        "created": 0,
        "updated": 1,
        "skipped": 0,
        "errors": [],
    }

    target_db = target_client.testing_sessionmaker()
    try:
        matching_cases = (
            target_db.query(DefectCase).filter(DefectCase.case_number == case.case_number).all()
        )
        assert len(matching_cases) == 1
        assert len(matching_cases[0].items) == 1
        assert len(matching_cases[0].photos) == 1
        assert len(matching_cases[0].status_history) == 2

        all_summaries = target_db.query(DailyProductionSummary).all()
        assert len(all_summaries) == 1
    finally:
        target_db.close()


# ---------------------------------------------------------------------------
# Auth: login required, plus a correct password
# ---------------------------------------------------------------------------


def test_import_requires_login(target_client):
    target_client.cookies.clear()
    resp = target_client.post(
        "/api/v1/admin/import-data",
        json={
            "password": TEST_PASSWORD,
            "defect_cases": [],
            "customer_issues": [],
            "daily_production_summaries": [],
        },
    )
    assert resp.status_code == 401


def test_import_with_wrong_password_is_rejected_and_imports_nothing(
    source_db, source_uploads_dir, target_client
):
    _build_source_data(source_db, source_uploads_dir)
    bundle = migration_service.export_bundle(source_db, source_uploads_dir)

    resp = target_client.post(
        "/api/v1/admin/import-data", json={**bundle, "password": "not-the-password"}
    )
    assert resp.status_code == 400

    target_db = target_client.testing_sessionmaker()
    try:
        assert target_db.query(DefectCase).count() == 0
        assert target_db.query(CustomerIssue).count() == 0
        assert target_db.query(DailyProductionSummary).count() == 0
    finally:
        target_db.close()


# ---------------------------------------------------------------------------
# A referenced master-data name that doesn't exist on the target: a clear
# per-record error, not a crash, and the rest of the import still proceeds.
# ---------------------------------------------------------------------------


def test_unknown_station_name_is_reported_per_record_without_aborting_the_import(
    source_db, source_uploads_dir, target_client
):
    case, _issue = _build_source_data(source_db, source_uploads_dir)
    bundle = migration_service.export_bundle(source_db, source_uploads_dir)
    bundle["defect_cases"][0]["found_station_name"] = "Station That Does Not Exist"

    resp = target_client.post(
        "/api/v1/admin/import-data", json={**bundle, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False

    case_result = body["defect_cases"]
    assert case_result["created"] == 0
    assert case_result["skipped"] == 1
    assert len(case_result["errors"]) == 1
    assert case_result["errors"][0]["key"] == case.case_number
    assert "Station That Does Not Exist" in case_result["errors"][0]["message"]

    # The customer issue linking to this now-missing case fails too (a real,
    # correctly-reported consequence), but the rest of the import - the daily
    # summary, which doesn't depend on the failed case - still goes through.
    assert body["customer_issues"]["skipped"] == 1
    assert body["daily_production_summaries"] == {
        "created": 1,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }

    target_db = target_client.testing_sessionmaker()
    try:
        assert target_db.query(DefectCase).count() == 0
    finally:
        target_db.close()
