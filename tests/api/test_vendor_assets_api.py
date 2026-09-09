"""Vendored-scan-asset-serving checks (jsQR) - part of the 2026-09-01 "scan
modal will not close, label is never read" hotfix's prevent-recurrence
coverage. See tests/unit/test_scan_vendor_assets.py for the companion "this
file exists on disk and isn't empty" check.

PHASE 9 REMOVAL (2026-09-09): this file used to also cover the Tesseract OCR
vendored assets and the /api/v1/scan/config, /parse-label, /diagnose
endpoints - all removed, along with app/routers/scan.py and
app/services/ocr_service.py, in favor of an always-present A-Z letter picker
for the work order line. QR decoding (jsQR / BarcodeDetector) is unchanged and
still fills the order number - see app/static/js/label-scan.js.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_jsqr_vendor_asset_is_served_successfully(client):
    resp = client.get("/static/js/vendor/jsqr.js")
    assert resp.status_code == 200, f"jsqr.js did not serve (HTTP {resp.status_code})"
    # A sane content type: present, and never the generic
    # "application/octet-stream" fallback Starlette uses when it can't guess
    # anything at all for a path (which would suggest a wrong/missing
    # extension rather than the real vendored file).
    content_type = resp.headers.get("content-type", "")
    assert content_type, "jsqr.js served with no Content-Type at all"
    assert "application/octet-stream" not in content_type
    assert len(resp.content) > 0


def test_jsqr_vendor_asset_is_reachable_without_a_login_session():
    """/static/* is exempt from LoginRequiredMiddleware (CLAUDE.md) - a phone
    scanning a label must be able to fetch this before/without an
    authenticated session cookie ever being an issue, same as any other static
    asset in this app."""
    anonymous_client = TestClient(app)
    resp = anonymous_client.get("/static/js/vendor/jsqr.js")
    assert (
        resp.status_code == 200
    ), f"jsqr.js requires auth - it shouldn't (HTTP {resp.status_code})"
