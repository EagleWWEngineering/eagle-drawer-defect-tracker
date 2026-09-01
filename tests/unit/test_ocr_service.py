"""PROJECT_SPEC_PHASE9.md Part 3: label field parsing + cross-field validation.

These exercise the pure Python functions in app/services/ocr_service.py directly
- the same functions both OCR paths (the default browser-side Tesseract.js path
via diagnose_scanned_label, and the optional cloud-provider path via
diagnose_label) share. No network call is made anywhere in this file except the
two provider-call tests at the bottom, which mock httpx.AsyncClient via
httpx.MockTransport (this repo's existing convention - see
tests/unit/test_sync_service.py) rather than making a real request.
"""

from __future__ import annotations

import httpx
import pytest

from app.errors import ServiceError
from app.services import ocr_service


def _mock_async_client(monkeypatch, handler):
    """Route every httpx.AsyncClient(...) constructed inside app/services/
    ocr_service.py through httpx.MockTransport(handler) instead of the network -
    same boundary this repo already mocks at (tests/unit/test_sync_service.py)."""

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _MockAsyncClient)


# ---------------------------------------------------------------------------
# A clean label parses every field
# ---------------------------------------------------------------------------


def _clean_label_lines() -> list[dict]:
    return [
        {"text": "#178414 [22] S", "x": 55, "y": 12},
        {"text": "Maple 3/4", "x": 55, "y": 40},
        {"text": "7 x 20.438 x 10.625", "x": 55, "y": 55},
        {"text": "3/4 | 7", "x": 90, "y": 90},
        {"text": "E", "x": 500, "y": 500},
    ]


def test_clean_label_parses_every_field():
    parsed = ocr_service.parse_label(_clean_label_lines(), qr_x=60, qr_y=60)
    assert parsed.order_number == "178414"
    assert parsed.quantity == 22
    assert parsed.ship_code == "S"
    assert parsed.line_label == "E"
    assert parsed.line_label_alternates == []
    assert parsed.line_label_used_centroid_fallback is False
    assert parsed.dimensions == {"height": 7.0, "width": 20.438, "depth": 10.625}
    assert parsed.thickness == "3/4"
    assert parsed.corner_block == {"thickness": "3/4", "height": 7.0}

    validation = ocr_service.validate_parsed_label(parsed, qr_order_number="178414")
    assert validation.failures == []
    assert validation.skipped == []


def test_diagnose_scanned_label_happy_path_matches_clean_label():
    result = ocr_service.diagnose_scanned_label(
        _clean_label_lines(), qr_order_number="178414", qr_x=60, qr_y=60
    )
    assert result["line_label"] == "E"
    assert result["line_label_discarded"] is False
    assert result["validator_failures"] == []
    assert result["elapsed_ms"] == 0.0


# ---------------------------------------------------------------------------
# The "Bot:" decoy line never becomes the drawer's own dimensions/thickness
# ---------------------------------------------------------------------------


def test_bot_decoy_line_never_becomes_drawer_dimensions():
    lines = [
        {"text": "Bot: 1/4  10.625 x 20.438 x 1.0", "x": 10, "y": 10},
        {"text": "5 x 6 x 7", "x": 10, "y": 30},
    ]
    assert ocr_service.parse_dimensions(lines) == {"height": 5.0, "width": 6.0, "depth": 7.0}


def test_bot_decoy_line_never_becomes_drawer_thickness():
    lines = [
        {"text": "Maple Bot: 1/4", "x": 10, "y": 10},
        {"text": "Oak 3/4", "x": 10, "y": 30},
    ]
    assert ocr_service.parse_thickness(lines) == "3/4"


def test_dimensions_and_thickness_are_none_when_only_a_decoy_line_matches():
    lines = [{"text": "Bot: 1/4  10.625 x 20.438 x 1.0", "x": 10, "y": 10}]
    assert ocr_service.parse_dimensions(lines) is None
    assert ocr_service.parse_thickness(lines) is None


# ---------------------------------------------------------------------------
# Line label candidates: length, blocklist, geometry ranking
# ---------------------------------------------------------------------------


def test_double_letter_line_label_is_found():
    lines = [{"text": "AB", "x": 500, "y": 500}]
    result = ocr_service.parse_line_label(lines, qr_x=0, qr_y=0)
    assert result["value"] == "AB"


def test_ship_code_and_line_label_sharing_a_letter_resolve_to_the_qr_distant_one():
    # Same letter twice: one right beside the QR (the ship code), one in the
    # corner diagonally opposite (the real line label). Ranking by distance from
    # the QR must still surface exactly one primary + the other as an alternate,
    # not silently dedupe/merge them.
    lines = [
        {"text": "D", "x": 62, "y": 62},  # ship code, right beside QR at (60, 60)
        {"text": "D", "x": 900, "y": 900},  # true line label, diagonally opposite
    ]
    result = ocr_service.parse_line_label(lines, qr_x=60, qr_y=60)
    assert result["value"] == "D"
    assert result["alternates"] == ["D"]


def test_line_label_ranking_picks_the_farther_candidate_regardless_of_letter():
    # A distinct letter for the far (true) line label makes the ranking
    # direction independently verifiable, not just "both happen to say D".
    lines = [
        {"text": "D", "x": 62, "y": 62},  # ship code, near the QR
        {"text": "A", "x": 900, "y": 900},  # true line label, far from the QR
    ]
    result = ocr_service.parse_line_label(lines, qr_x=60, qr_y=60)
    assert result["value"] == "A"
    assert result["alternates"] == ["D"]

    # Order of the input lines must not matter - same result reversed.
    result_reversed = ocr_service.parse_line_label(list(reversed(lines)), qr_x=60, qr_y=60)
    assert result_reversed["value"] == "A"


@pytest.mark.parametrize("blocked", ["SS", "FH"])
def test_ss_and_fh_are_rejected_as_line_labels(blocked):
    lines = [{"text": blocked, "x": 500, "y": 500}]
    result = ocr_service.parse_line_label(lines, qr_x=0, qr_y=0)
    assert result["value"] is None
    assert result["alternates"] == []


def test_ss_and_fh_do_not_crowd_out_a_real_candidate():
    lines = [
        {"text": "SS", "x": 10, "y": 10},
        {"text": "FH", "x": 20, "y": 20},
        {"text": "C", "x": 500, "y": 500},
    ]
    result = ocr_service.parse_line_label(lines, qr_x=0, qr_y=0)
    assert result["value"] == "C"


# ---------------------------------------------------------------------------
# Corner block separator OCR misreads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("separator", ["|", "I", "l", "L", "!"])
def test_corner_block_separator_misread_still_parses(separator):
    lines = [{"text": f"1/2 {separator} 7", "x": 10, "y": 10}]
    assert ocr_service.parse_corner_block(lines) == {"thickness": "1/2", "height": 7.0}


def test_corner_block_falls_back_to_joined_text_when_split_across_lines():
    lines = [{"text": "1/2 |", "x": 10, "y": 10}, {"text": "7", "x": 12, "y": 11}]
    assert ocr_service.parse_corner_block(lines) == {"thickness": "1/2", "height": 7.0}


# ---------------------------------------------------------------------------
# Validate against the QR: an order-number mismatch discards the OCR result
# ---------------------------------------------------------------------------


def test_order_number_mismatch_against_qr_is_a_validator_failure():
    lines = [{"text": "#178414 [22] S", "x": 55, "y": 12}]
    parsed = ocr_service.parse_label(lines)
    validation = ocr_service.validate_parsed_label(parsed, qr_order_number="999999")
    assert any("does not match the QR" in f for f in validation.failures)


def test_order_number_mismatch_discards_the_line_label():
    lines = _clean_label_lines()
    result = ocr_service.diagnose_scanned_label(lines, qr_order_number="999999", qr_x=60, qr_y=60)
    assert result["line_label"] is None
    assert result["line_label_alternates"] == []
    assert result["line_label_discarded"] is True
    assert any("does not match the QR" in f for f in result["validator_failures"])


def test_order_number_match_never_discards_the_line_label():
    result = ocr_service.diagnose_scanned_label(
        _clean_label_lines(), qr_order_number="178414", qr_x=60, qr_y=60
    )
    assert result["line_label"] == "E"
    assert result["line_label_discarded"] is False


# ---------------------------------------------------------------------------
# Hotfix (2026-09-01, "line label never fills"): a single misread digit in the
# order number - plausible OCR noise from a dimension crop that grazes the
# real order number printed beside the QR - must not discard an otherwise-good
# line-label read. Two or more differing digits is no longer plausibly noise.
# ---------------------------------------------------------------------------


def test_single_digit_order_number_difference_is_tolerated_not_discarded():
    lines = [{"text": "#178415 [22] S", "x": 55, "y": 12}, {"text": "E", "x": 500, "y": 500}]
    result = ocr_service.diagnose_scanned_label(lines, qr_order_number="178414", qr_x=60, qr_y=60)
    assert result["line_label"] == "E"
    assert result["line_label_discarded"] is False
    assert result["validator_failures"] == []


def test_single_digit_order_number_difference_is_skipped_not_a_pass_or_a_failure():
    parsed = ocr_service.parse_label([{"text": "#178415 [22] S", "x": 10, "y": 10}])
    validation = ocr_service.validate_parsed_label(parsed, qr_order_number="178414")
    assert validation.failures == []
    assert "order_number_matches_qr" in validation.skipped


def test_two_digit_order_number_difference_still_discards():
    # "178919" vs "178414": differs at exactly 2 positions (index 3 and 5).
    lines = [{"text": "#178919 [22] S", "x": 55, "y": 12}, {"text": "E", "x": 500, "y": 500}]
    result = ocr_service.diagnose_scanned_label(lines, qr_order_number="178414", qr_x=60, qr_y=60)
    assert result["line_label"] is None
    assert result["line_label_discarded"] is True
    assert any("does not match the QR" in f for f in result["validator_failures"])


# ---------------------------------------------------------------------------
# Hotfix (2026-09-01): raw OCR text with trailing whitespace/newlines, and a
# genuinely single-character line label, must still parse.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw_text", ["E", "E\n", "E\r\n", "  E  ", "E\n\n"])
def test_line_label_with_trailing_whitespace_or_newline_still_parses(raw_text):
    lines = [{"text": raw_text, "x": 500, "y": 500}]
    result = ocr_service.parse_line_label(lines, qr_x=0, qr_y=0)
    assert result["value"] == "E"


def test_single_character_line_label_parses_on_its_own():
    lines = [{"text": "B", "x": 500, "y": 500}]
    result = ocr_service.parse_line_label(lines, qr_x=0, qr_y=0)
    assert result["value"] == "B"


# ---------------------------------------------------------------------------
# A missing field yields a SKIPPED check, never a silent pass
# ---------------------------------------------------------------------------


def test_missing_corner_block_is_skipped_not_passed():
    parsed = ocr_service.parse_label([{"text": "5 x 6 x 7", "x": 10, "y": 10}])
    validation = ocr_service.validate_parsed_label(parsed, qr_order_number="178414")
    assert "corner_block_height_matches_dimensions" in validation.skipped
    assert "corner_block_thickness_matches_species_line" in validation.skipped
    assert validation.failures == []


def test_missing_order_number_on_either_side_is_skipped_not_passed():
    parsed = ocr_service.parse_label([{"text": "no order number here", "x": 10, "y": 10}])
    validation = ocr_service.validate_parsed_label(parsed, qr_order_number="178414")
    assert "order_number_matches_qr" in validation.skipped

    parsed_with_order = ocr_service.parse_label([{"text": "#178414", "x": 10, "y": 10}])
    validation_no_qr = ocr_service.validate_parsed_label(parsed_with_order, qr_order_number=None)
    assert "order_number_matches_qr" in validation_no_qr.skipped


def test_unreadable_order_number_does_not_discard_an_otherwise_valid_line_read():
    """An order number that never parsed at all (not merely a noisy read) must
    be UNVERIFIED, never treated as a mismatch - PROJECT_SPEC_PHASE9.md hotfix
    2, Step 4."""
    lines = [
        {"text": "no readable order number on this crop", "x": 10, "y": 10},
        {"text": "E", "x": 500, "y": 500},
    ]
    result = ocr_service.diagnose_scanned_label(lines, qr_order_number="178414", qr_x=60, qr_y=60)
    assert result["line_label"] == "E"
    assert result["line_label_discarded"] is False


def test_mismatched_corner_block_height_is_a_failure():
    lines = [
        {"text": "5 x 6 x 7", "x": 10, "y": 10},
        {"text": "1/2 | 99", "x": 10, "y": 30},
    ]
    parsed = ocr_service.parse_label(lines)
    validation = ocr_service.validate_parsed_label(parsed, qr_order_number=None)
    assert any("Corner block height" in f for f in validation.failures)


def test_mismatched_corner_block_thickness_is_a_failure():
    lines = [
        {"text": "Maple 1/2", "x": 10, "y": 10},
        {"text": "5 x 6 x 7", "x": 10, "y": 20},
        {"text": "3/4 | 5", "x": 10, "y": 30},
    ]
    parsed = ocr_service.parse_label(lines)
    validation = ocr_service.validate_parsed_label(parsed, qr_order_number=None)
    assert any("Corner block thickness" in f for f in validation.failures)


# ---------------------------------------------------------------------------
# Provider calls (httpx boundary mocked - see _mock_async_client above)
# ---------------------------------------------------------------------------


async def test_call_provider_rejects_tesseract_and_unknown_names():
    with pytest.raises(ocr_service.OcrProviderError):
        await ocr_service.call_provider(b"x", provider="tesseract", endpoint="", api_key="k")
    with pytest.raises(ocr_service.OcrProviderError):
        await ocr_service.call_provider(b"x", provider="bogus", endpoint="", api_key="k")


async def test_call_anthropic_provider_parses_strict_json(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "real-key"
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": (
                            '{"order_number": "178414", "quantity": 22, "ship_code": "S", '
                            '"line_label": "E", "dimensions": {"height": 7, "width": 20.4, '
                            '"depth": 10.6}, "thickness": "3/4", '
                            '"corner_block": {"thickness": "3/4", "height": 7}}'
                        ),
                    }
                ]
            },
        )

    _mock_async_client(monkeypatch, handler)
    fields = await ocr_service.call_anthropic_provider(b"fake-jpeg-bytes", api_key="real-key")
    assert fields["order_number"] == "178414"
    assert fields["line_label"] == "E"


async def test_call_anthropic_provider_requires_a_key():
    with pytest.raises(ocr_service.OcrProviderError):
        await ocr_service.call_anthropic_provider(b"x", api_key="")


async def test_call_anthropic_provider_rejects_non_json_response(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "not json"}]})

    _mock_async_client(monkeypatch, handler)
    with pytest.raises(ocr_service.OcrProviderError):
        await ocr_service.call_anthropic_provider(b"x", api_key="k")


async def test_call_anthropic_provider_surfaces_http_errors(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    _mock_async_client(monkeypatch, handler)
    with pytest.raises(ocr_service.OcrProviderError):
        await ocr_service.call_anthropic_provider(b"x", api_key="bad-key")


async def test_diagnose_label_wraps_provider_errors_as_service_error(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _mock_async_client(monkeypatch, handler)
    with pytest.raises(ServiceError):
        await ocr_service.diagnose_label(
            b"x",
            provider="azure",
            endpoint="https://example.cognitiveservices.azure.com",
            api_key="k",
            qr_order_number=None,
            qr_x=None,
            qr_y=None,
        )


async def test_diagnose_label_anthropic_path_never_touches_lines_pipeline(monkeypatch):
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
    result = await ocr_service.diagnose_label(
        b"x",
        provider="anthropic",
        endpoint="",
        api_key="k",
        qr_order_number="178414",
        qr_x=None,
        qr_y=None,
    )
    assert result["line_label"] == "E"
    assert result["line_label_alternates"] == []
    assert result["line_label_used_centroid_fallback"] is False
    assert result["raw_lines"] == []
