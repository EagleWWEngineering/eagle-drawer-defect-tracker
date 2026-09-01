"""PROJECT_SPEC_PHASE9.md Part 3: label-scan endpoints.

- GET /api/v1/scan/config tells the client which engine to use.
- POST /api/v1/scan/parse-label is the default, browser-side (Tesseract.js)
  path - pure text parsing, no provider call, no OCR_API_KEY required.
- POST /api/v1/scan/diagnose is the optional cloud-provider path
  (azure/google/anthropic) - provider calls are mocked at the httpx boundary
  (httpx.MockTransport), never a real network call.
- The vendored-scan-asset-serving checks near the bottom of this file are part
  of the 2026-09-01 "scan modal will not close, label is never read" hotfix's
  prevent-recurrence coverage - see tests/unit/test_scan_vendor_assets.py for
  the companion "these files exist on disk and aren't empty" checks.
"""

from __future__ import annotations

import io

import httpx
import pytest

from app import config


def _mock_async_client(monkeypatch, handler):
    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _MockAsyncClient)


def _clean_label_lines() -> list[dict]:
    return [
        {"text": "#178414 [22] S", "x": 55, "y": 12},
        {"text": "Maple 3/4", "x": 55, "y": 40},
        {"text": "7 x 20.438 x 10.625", "x": 55, "y": 55},
        {"text": "3/4 | 7", "x": 90, "y": 90},
        {"text": "E", "x": 500, "y": 500},
    ]


# ---------------------------------------------------------------------------
# GET /config
# ---------------------------------------------------------------------------


def test_scan_config_defaults_to_tesseract_enabled(client):
    resp = client.get("/api/v1/scan/config")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "provider": "tesseract"}


def test_scan_config_reflects_ocr_disabled(client, monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "ocr_enabled", False)
    resp = client.get("/api/v1/scan/config")
    assert resp.json()["enabled"] is False


# ---------------------------------------------------------------------------
# POST /parse-label - the tesseract path requires no key
# ---------------------------------------------------------------------------


def test_parse_label_requires_no_api_key(client, monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "ocr_api_key", "")
    monkeypatch.setattr(settings, "ocr_provider", "tesseract")
    resp = client.post(
        "/api/v1/scan/parse-label",
        json={"lines": _clean_label_lines(), "qr_order_number": "178414", "qr_x": 60, "qr_y": 60},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["line_label"] == "E"
    assert body["line_label_discarded"] is False


def test_parse_label_discards_line_on_order_number_mismatch(client):
    resp = client.post(
        "/api/v1/scan/parse-label",
        json={"lines": _clean_label_lines(), "qr_order_number": "999999", "qr_x": 60, "qr_y": 60},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["line_label"] is None
    assert body["line_label_discarded"] is True


def test_parse_label_works_with_no_qr_position_at_all(client):
    resp = client.post("/api/v1/scan/parse-label", json={"lines": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["line_label"] is None
    assert body["validator_skipped"]


def test_parse_label_503s_when_ocr_disabled(client, monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "ocr_enabled", False)
    resp = client.post("/api/v1/scan/parse-label", json={"lines": []})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST /diagnose - cloud providers only, require OCR_API_KEY
# ---------------------------------------------------------------------------


def test_diagnose_503s_when_cloud_provider_selected_without_a_key(client, monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "ocr_provider", "azure")
    monkeypatch.setattr(settings, "ocr_api_key", "")
    resp = client.post(
        "/api/v1/scan/diagnose",
        files={"image": ("label.jpg", io.BytesIO(b"\xff\xd8\xff" + b"0" * 100), "image/jpeg")},
    )
    assert resp.status_code == 503


def test_diagnose_503s_when_ocr_disabled_even_with_a_key(client, monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "ocr_enabled", False)
    monkeypatch.setattr(settings, "ocr_provider", "azure")
    monkeypatch.setattr(settings, "ocr_api_key", "real-key")
    resp = client.post(
        "/api/v1/scan/diagnose",
        files={"image": ("label.jpg", io.BytesIO(b"\xff\xd8\xff" + b"0" * 100), "image/jpeg")},
    )
    assert resp.status_code == 503


def test_diagnose_oversized_upload_is_rejected(client, monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "ocr_provider", "azure")
    monkeypatch.setattr(settings, "ocr_api_key", "real-key")
    big_bytes = b"\xff\xd8\xff" + b"0" * (9 * 1024 * 1024)
    resp = client.post(
        "/api/v1/scan/diagnose",
        files={"image": ("big.jpg", io.BytesIO(big_bytes), "image/jpeg")},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "image"


def test_diagnose_happy_path_with_azure_mocked(client, monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "ocr_provider", "azure")
    monkeypatch.setattr(settings, "ocr_api_key", "real-key")
    monkeypatch.setattr(settings, "ocr_endpoint", "https://fake.cognitiveservices.azure.com")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "readResult": {
                    "blocks": [
                        {
                            "lines": [
                                {
                                    "text": "#178414 [22] S",
                                    "boundingPolygon": [{"x": 55, "y": 12}],
                                },
                                {"text": "E", "boundingPolygon": [{"x": 500, "y": 500}]},
                            ]
                        }
                    ]
                }
            },
        )

    _mock_async_client(monkeypatch, handler)
    resp = client.post(
        "/api/v1/scan/diagnose",
        data={"qr_order_number": "178414", "qr_x": "60", "qr_y": "60"},
        files={"image": ("label.jpg", io.BytesIO(b"\xff\xd8\xff" + b"0" * 100), "image/jpeg")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["order_number"] == "178414"
    assert body["line_label"] == "E"


def test_diagnose_provider_error_surfaces_through_standard_error_envelope(client, monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "ocr_provider", "azure")
    monkeypatch.setattr(settings, "ocr_api_key", "real-key")
    monkeypatch.setattr(settings, "ocr_endpoint", "https://fake.cognitiveservices.azure.com")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    _mock_async_client(monkeypatch, handler)
    resp = client.post(
        "/api/v1/scan/diagnose",
        files={"image": ("label.jpg", io.BytesIO(b"\xff\xd8\xff" + b"0" * 100), "image/jpeg")},
    )
    assert resp.status_code == 400
    assert "error" in resp.json()
    assert "HTTP 500" in resp.json()["error"]["message"]


def test_diagnose_with_anthropic_provider_mocked(client, monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "ocr_provider", "anthropic")
    monkeypatch.setattr(settings, "ocr_api_key", "real-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": '{"order_number": "178414", "line_label": "E"}',
                    }
                ]
            },
        )

    _mock_async_client(monkeypatch, handler)
    resp = client.post(
        "/api/v1/scan/diagnose",
        data={"qr_order_number": "178414"},
        files={"image": ("label.jpg", io.BytesIO(b"\xff\xd8\xff" + b"0" * 100), "image/jpeg")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["line_label"] == "E"


# ---------------------------------------------------------------------------
# Vendored scan assets are actually SERVED correctly (hotfix 2026-09-01) - see
# tests/unit/test_scan_vendor_assets.py for the "exists on disk" half of this.
# ---------------------------------------------------------------------------

VENDOR_STATIC_PATHS: tuple[str, ...] = (
    "/static/js/vendor/jsqr.js",
    "/static/js/vendor/tesseract.min.js",
    "/static/js/vendor/tesseract-worker.min.js",
    "/static/js/vendor/tesseract-core-lstm.wasm.js",
    "/static/js/vendor/eng.traineddata.gz",
)


@pytest.mark.parametrize("path", VENDOR_STATIC_PATHS)
def test_vendored_scan_asset_is_served_successfully(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} did not serve (HTTP {resp.status_code})"
    # A sane content type: present, and never the generic
    # "application/octet-stream" fallback Starlette uses when it can't guess
    # anything at all for a path (which would suggest a wrong/missing
    # extension rather than the real vendored file).
    content_type = resp.headers.get("content-type", "")
    assert content_type, f"{path} served with no Content-Type at all"
    assert "application/octet-stream" not in content_type
    assert len(resp.content) > 0


def test_vendored_scan_assets_are_reachable_without_a_login_session():
    """/static/* is exempt from LoginRequiredMiddleware (CLAUDE.md) - a phone
    scanning a label must be able to fetch these before/without an
    authenticated session cookie ever being an issue, same as any other static
    asset in this app."""
    from fastapi.testclient import TestClient

    from app.main import app

    anonymous_client = TestClient(app)
    for path in VENDOR_STATIC_PATHS:
        resp = anonymous_client.get(path)
        assert (
            resp.status_code == 200
        ), f"{path} requires auth - it shouldn't (HTTP {resp.status_code})"


def test_javascript_vendor_assets_are_not_double_gzip_encoded(client):
    """The eng.traineddata.gz language file must reach the browser as raw gzip
    bytes for Tesseract.js's own gunzip step to work - if Starlette ever
    started setting Content-Encoding: gzip for it, the browser's fetch() would
    transparently decompress it first and Tesseract's gunzip would then fail
    on already-decompressed bytes (a real, silent failure mode considered
    during this hotfix - see app/services/ocr_service.py's module docstring
    history / the hotfix commit message for the investigation). Confirmed:
    Starlette's FileResponse never sets Content-Encoding from mimetypes.guess_type's
    encoding element, only its type element - this test pins that behavior."""
    resp = client.get("/static/js/vendor/eng.traineddata.gz")
    assert resp.status_code == 200
    assert "content-encoding" not in {k.lower() for k in resp.headers.keys()}
    # Raw gzip bytes start with the magic number 0x1f 0x8b.
    assert resp.content[:2] == b"\x1f\x8b"
