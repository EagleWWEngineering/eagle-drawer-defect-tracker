"""PROJECT_SPEC_PHASE9.md Part 3: label-scan endpoints.

- GET /api/v1/scan/config tells the client which engine to use.
- POST /api/v1/scan/parse-label is the default, browser-side (Tesseract.js)
  path - pure text parsing, no provider call, no OCR_API_KEY required.
- POST /api/v1/scan/diagnose is the optional cloud-provider path
  (azure/google/anthropic) - provider calls are mocked at the httpx boundary
  (httpx.MockTransport), never a real network call.
"""

from __future__ import annotations

import io

import httpx

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
