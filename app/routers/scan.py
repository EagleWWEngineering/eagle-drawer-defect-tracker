"""Label-scan OCR (PROJECT_SPEC_PHASE9.md Part 3). HTTP input/output only; every
parsing and validation rule lives in app/services/ocr_service.py (existing
layering rule).

Three routes:

  - GET /config - tells the New Defect form's scan button which engine is active
    (app/static/js/label-scan.js), so it knows whether to run Tesseract.js
    client-side or fall back to capturing a photo for a cloud provider.
  - POST /parse-label - the default, browser-side path: Tesseract.js already ran
    on the phone, this just parses/validates the raw text it recognised. Never
    writes to the database, never calls a network provider, never requires
    OCR_API_KEY - only the OCR_ENABLED kill switch gates it.
  - POST /diagnose - the optional cloud-provider path (azure/google/anthropic),
    off by default. Diagnostic in nature: no `db: Session` parameter anywhere in
    this file, on purpose, so that stays true even if someone later edits this
    route carelessly.

`LoginRequiredMiddleware` gates every route in this file like every other route
in the app (see app/main.py / app/auth_middleware.py) - none of them are added to
that middleware's public allowlist.
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.config import get_settings
from app.errors import ValidationError
from app.schemas import ScanConfigOut, ScanDiagnosticOut, ScanParseIn
from app.services import ocr_service

router = APIRouter(prefix="/api/v1/scan", tags=["scan"])


@router.get("/config", response_model=ScanConfigOut)
def get_scan_config() -> ScanConfigOut:
    """Which OCR engine the New Defect form's scan button should use. Not a
    secret - OCR_API_KEY is never exposed here, only which provider is selected
    and whether OCR is enabled at all (QR decoding and manual entry work either
    way - see app/static/js/label-scan.js)."""
    settings = get_settings()
    return ScanConfigOut(enabled=settings.ocr_enabled, provider=settings.ocr_provider)


@router.post("/parse-label", response_model=ScanDiagnosticOut)
def parse_label(payload: ScanParseIn) -> ScanDiagnosticOut:
    """Parse + validate raw OCR text lines Tesseract.js already recognised in the
    browser (the default path - see app/services/ocr_service.py
    diagnose_scanned_label). No image is uploaded here and no provider is called -
    this is pure text parsing, so it requires no OCR_API_KEY and works out of the
    box with zero configuration. Still respects the OCR_ENABLED kill switch: with
    OCR disabled, the New Defect form falls back to QR-only + manual entry for
    the line, same as the cloud-provider path below.
    """
    settings = get_settings()
    if not settings.ocr_enabled:
        raise HTTPException(
            status_code=503,
            detail="OCR is disabled on this server (OCR_ENABLED=false). Enter the line manually.",
        )
    result = ocr_service.diagnose_scanned_label(
        [line.model_dump() for line in payload.lines],
        qr_order_number=payload.qr_order_number,
        qr_x=payload.qr_x,
        qr_y=payload.qr_y,
    )
    return ScanDiagnosticOut(**result)


@router.post("/diagnose", response_model=ScanDiagnosticOut)
async def diagnose(
    image: UploadFile = File(...),
    qr_order_number: str | None = Form(default=None),
    qr_x: float | None = Form(default=None),
    qr_y: float | None = Form(default=None),
) -> ScanDiagnosticOut:
    """Parse one photographed label via the configured cloud OCR provider
    (azure/google/anthropic - never "tesseract", which never reaches this route
    at all) and validate the result against the browser's own (client-decoded)
    QR read.

    `qr_order_number`/`qr_x`/`qr_y` are all optional - the browser sends them when
    its own QR decode succeeded, but a frame with no readable QR is still worth
    diagnosing (order-number cross-check and the line-label geometry's QR-position
    input both degrade gracefully to "skipped"/"centroid fallback" rather than
    failing the whole request - see app/services/ocr_service.py).

    503, not a generic error, when OCR isn't usable at all (disabled, or no API
    key configured) - so the page can render an honest "OCR is disabled" state
    instead of a confusing failure. Images are never written to disk - UPLOADS_DIR
    stays for defect photos only; this handler only ever holds the bytes in memory
    long enough to forward them to the provider.
    """
    settings = get_settings()
    if not settings.ocr_enabled or not settings.ocr_api_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "OCR is disabled on this server. Set OCR_ENABLED=true, OCR_PROVIDER to a cloud "
                "provider, and a real OCR_API_KEY to use the label-scan diagnostic."
            ),
        )

    contents = await image.read()
    if len(contents) == 0:
        raise ValidationError("Uploaded image is empty.", field="image")
    if len(contents) > settings.max_upload_bytes:
        raise ValidationError(
            f"Image exceeds the {settings.max_upload_mb} MB limit.", field="image"
        )

    result = await ocr_service.diagnose_label(
        contents,
        provider=settings.ocr_provider,
        endpoint=settings.ocr_endpoint,
        api_key=settings.ocr_api_key,
        qr_order_number=qr_order_number,
        qr_x=qr_x,
        qr_y=qr_y,
    )
    return ScanDiagnosticOut(**result)
