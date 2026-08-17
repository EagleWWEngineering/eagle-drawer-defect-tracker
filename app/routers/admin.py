"""TEMPORARY, ONE-TIME migration endpoint.

`POST /api/v1/admin/import-data` exists solely to move real production data
(defect cases, customer issues, daily summaries, photos) from Rodolfo's local
dev SQLite database to the live Render deployment's database, once, over
HTTPS, triggered by Rodolfo himself with his real login. See
app/services/migration_service.py for the natural-key remapping logic and
scripts/export_real_data.py for the local export script that produces the
request body.

This route, app/services/migration_service.py, and scripts/export_real_data.py
are all slated for REMOVAL in a follow-up commit once Rodolfo has manually
confirmed the real migration landed correctly on Render - do not build new
functionality on top of this endpoint.

Auth: protected like every other route by LoginRequiredMiddleware (a valid
session cookie is required just to reach this route at all), PLUS this route
additionally requires re-entering the current shared password in the request
body - the exact same "confirm a destructive action by re-entering the
password" pattern already used by POST /api/v1/auth/logout-everywhere (see
app/routers/auth.py / app/services/auth_service.py verify_credentials). Wrong
password -> 400, nothing imported, no sessions touched.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dependencies import get_db
from app.errors import ValidationError
from app.services import auth_service, migration_service

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class ImportDataIn(BaseModel):
    # Re-entering the current shared password is required to confirm this
    # data-changing action, mirroring LogoutEverywhereIn in app/routers/auth.py.
    password: str
    defect_cases: list[dict[str, Any]] = Field(default_factory=list)
    customer_issues: list[dict[str, Any]] = Field(default_factory=list)
    daily_production_summaries: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"extra": "ignore"}


class TableImportResult(BaseModel):
    created: int
    updated: int
    skipped: int
    errors: list[dict[str, str]]


class ImportDataOut(BaseModel):
    ok: bool
    defect_cases: TableImportResult
    customer_issues: TableImportResult
    daily_production_summaries: TableImportResult


@router.post("/import-data", response_model=ImportDataOut)
def import_data(payload: ImportDataIn, db: Session = Depends(get_db)) -> ImportDataOut:
    """Import a scripts/export_real_data.py export bundle into this database.

    Safe to re-run: every table is matched on its natural key (DefectCase by
    case_number, CustomerIssue by issue_number, DailyProductionSummary by
    (production_date, shift)) and updated in place rather than duplicated, so a
    partially-failed attempt can simply be re-submitted.
    """
    if not auth_service.verify_credentials(db, auth_service.get_app_username(db), payload.password):
        raise ValidationError("Incorrect password.", field="password")

    settings = get_settings()
    bundle = payload.model_dump(exclude={"password"})
    result = migration_service.import_bundle(db, settings.uploads_dir, bundle)

    ok = all(not table["errors"] for table in result.values())
    return ImportDataOut(ok=ok, **result)
