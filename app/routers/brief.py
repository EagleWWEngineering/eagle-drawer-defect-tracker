"""Brief Export (Part A): serves the Eagle production brief's drawers TV board
its ~06:15 ET daily fetch - GET /api/v1/brief/summary. Read-only, gated by its
own X-Brief-Key header (never the shared login) - see
app/auth_middleware.py PUBLIC_EXACT_PATHS for its allowlist exemption.

HTTP input/output only - all business logic lives in
app/services/brief_export_service.py.
"""

from __future__ import annotations

import datetime as dt
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dependencies import get_db
from app.errors import ValidationError
from app.schemas import BriefSummaryOut
from app.services import brief_export_service
from app.timezone_utils import today_in_display_timezone

router = APIRouter(prefix="/api/v1/brief", tags=["brief"])


def _verify_brief_key(x_brief_key: str | None) -> None:
    """Constant-time comparison against BRIEF_API_KEY - a secret deliberately
    separate from RELAY_API_KEY (app/routers/sync.py _verify_relay_key): a
    different machine (the production brief's VM, pulling FROM this app)
    calling in the opposite direction, so revoking one must never break the
    other. Missing/wrong key -> 401, same shape as the relay endpoints'
    _verify_relay_key. An unset/empty BRIEF_API_KEY rejects every request
    (fails closed) rather than allowing unauthenticated access."""
    expected = get_settings().brief_api_key
    if not expected or not x_brief_key or not secrets.compare_digest(x_brief_key, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid brief key.")


@router.get("/summary", response_model=BriefSummaryOut)
def get_brief_summary(
    product: str | None = None,
    asof: str | None = None,
    x_brief_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> BriefSummaryOut:
    """See docs (the Brief Export prompt) for the full response shape.
    `product` is effectively required (validated against
    brief_export_service.SUPPORTED_PRODUCTS, currently only "drawers") but is
    deliberately NOT a plain required FastAPI parameter - it defaults to None
    so an omitted value reaches validate_product() and gets this app's
    standard 400 error envelope (app/errors.py) instead of FastAPI's own 422
    "missing query parameter" shape, matching how a wrong value (e.g.
    "doors") is rejected. `asof` defaults to today in DISPLAY_TIMEZONE; a
    malformed value is also a 400 in that same standard envelope - unlike
    this app's other `dt.date`-typed query params (e.g. start_date/end_date
    elsewhere), asof is accepted here as a plain string and parsed by hand
    specifically so a bad value from an external caller (the production
    brief's generator) reads the same way every other business-rule failure
    in this app does.
    """
    _verify_brief_key(x_brief_key)
    brief_export_service.validate_product(product)

    if asof is None:
        asof_date = today_in_display_timezone()
    else:
        try:
            asof_date = dt.date.fromisoformat(asof)
        except ValueError as exc:
            raise ValidationError(f"asof must be YYYY-MM-DD, got {asof!r}.", field="asof") from exc

    generated_at = dt.datetime.now(dt.timezone.utc)
    payload = brief_export_service.build_brief_summary(
        db, product=product, asof=asof_date, generated_at=generated_at
    )
    return BriefSummaryOut(**payload)
