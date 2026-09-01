"""Phase 9 Part 3 (originally scaffolded as Phase 8a's label-scan OCR diagnostic) —
provider abstraction, label field parsing, and cross-field validators. Read-only
and DB-write-free by design - nothing in this module touches the database, and it
never imports Request/Response objects, matching the existing
UI -> API router -> service layer -> DB layering rule (app/services/defect_service.py
is the model this file follows).

Two OCR paths exist, both ending up here:

  - The default, `tesseract`: Tesseract.js runs entirely in the browser (see
    app/static/js/label-scan.js) against two small crops derived from the QR
    code's geometry, and posts its RAW recognised text lines to
    POST /api/v1/scan/parse-label (app/routers/scan.py) for parsing/validation
    only - diagnose_scanned_label() below never calls a network provider, which
    is why it needs no OCR_API_KEY and works with zero configuration.
  - The optional cloud providers (`azure`, `google`, `anthropic`), behind
    POST /api/v1/scan/diagnose, off by default: this module calls the provider
    with the photographed image and does the same parsing/validation.

Every drawer label carries two independent reads of the same order number: a QR
code (decoded client-side) and printed text (read by whichever OCR path is
active). This module's job is turning that OCR read into the same handful of
fields every time, then checking those fields against each other and against the
QR's independent read - discarding the line label entirely when the order number
disagrees (see _line_label_discarded).

Failure behaviour for the cloud-provider path is deliberately "fail loud": a
provider error, timeout, or response this module cannot make sense of raises
ServiceError (app/errors.py) so it surfaces through the app's existing uniform
JSON error envelope. This module never returns a partial or zeroed-out result
silently - a caller either gets a real result or a clear error, never something
in between that looks like a real result.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field

import httpx

from app.errors import ServiceError

# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------
#
# The two lines-based cloud backends are normalised to the exact same shape
# before any parsing logic ever sees the result: list[dict] of {"text": str,
# "x": float, "y": float}, x/y being the centroid of that line's bounding
# polygon in the image's own pixel coordinates. Nothing past this point ever
# branches on which provider produced a given line list. `anthropic` is
# different - it returns the extracted fields directly (see
# call_anthropic_provider) since a vision-language model can take positional
# instructions a traditional OCR engine can't, so it never goes through the
# lines/parse_label pipeline at all (see diagnose_label).

#: Every value OCR_PROVIDER may be set to. "tesseract" (the default) never
#: reaches call_provider()/call_anthropic_provider() below - it's handled
#: entirely client-side plus diagnose_scanned_label()'s pure parsing.
OCR_PROVIDERS: tuple[str, ...] = ("tesseract", "anthropic", "azure", "google")

_AZURE_ANALYZE_PATH = "/computervision/imageanalysis:analyze"
_AZURE_API_VERSION = "2024-02-01"
_GOOGLE_ANNOTATE_URL = "https://vision.googleapis.com/v1/images:annotate"
_ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_API_VERSION = "2023-06-01"
_ANTHROPIC_MODEL = "claude-sonnet-5"
_ANTHROPIC_PROMPT = (
    "This is a photo of a drawer production work order label. Respond with ONLY "
    "strict JSON (no markdown formatting, no prose before or after) using exactly "
    "these keys: "
    '{"order_number": <6-digit string or null>, "quantity": <integer or null>, '
    '"ship_code": <single letter "D"/"S"/"P" or null>, '
    '"line_label": <1-2 letter work order line code or null>, '
    '"dimensions": {"height": <number>, "width": <number>, "depth": <number>} '
    "or null, "
    '"thickness": <fraction string like "1/2" or null>, '
    '"corner_block": {"thickness": <fraction string>, "height": <number>} or null}. '
    "The line label is the isolated 1-2 letter code printed in the label's corner "
    "diagonally OPPOSITE the QR code. It is NOT the ship code (D/S/P), which is "
    "printed beside the QR code - the two can coincidentally be the same letter, "
    "but they are different fields; use position, not the letter itself, to tell "
    "them apart. Ignore any line beginning with 'Bot:' entirely - that describes "
    "the bottom panel, not the drawer, and must never be reported as this "
    "drawer's own dimensions or thickness."
)

OCR_TIMEOUT_SECONDS = 30.0


class OcrProviderError(RuntimeError):
    """Raised for any failure calling or making sense of the OCR provider's
    response. Caught by diagnose_label() below and re-raised as a ServiceError -
    this internal type exists only so provider-call code doesn't need to import
    app/errors.py's app-wide error hierarchy directly."""


def _polygon_centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    if not points:
        raise OcrProviderError("OCR response contained a text line with no bounding polygon.")
    x = sum(p[0] for p in points) / len(points)
    y = sum(p[1] for p in points) / len(points)
    return x, y


def normalize_azure_response(data: dict) -> list[dict]:
    """readResult.blocks[].lines[] -> our normalised line list. See
    https://learn.microsoft.com/azure/ai-services/computer-vision/ - the `read`
    feature's response shape."""
    try:
        blocks = data["readResult"]["blocks"]
    except (KeyError, TypeError) as exc:
        raise OcrProviderError("Azure response is missing readResult.blocks.") from exc

    lines: list[dict] = []
    for block in blocks:
        for line in block.get("lines", []):
            polygon = line.get("boundingPolygon") or []
            points = [(pt["x"], pt["y"]) for pt in polygon]
            x, y = _polygon_centroid(points)
            lines.append({"text": line.get("text", ""), "x": x, "y": y})
    return lines


def normalize_google_response(data: dict) -> list[dict]:
    """responses[0].textAnnotations[] -> our normalised line list. Index 0 of
    textAnnotations is the whole-image concatenation, not a line - always skipped."""
    try:
        responses = data["responses"]
    except (KeyError, TypeError) as exc:
        raise OcrProviderError("Google response is missing 'responses'.") from exc
    if not responses:
        raise OcrProviderError("Google response's 'responses' list is empty.")

    annotations = responses[0].get("textAnnotations") or []
    lines: list[dict] = []
    for annotation in annotations[1:]:
        vertices = annotation.get("boundingPoly", {}).get("vertices") or []
        points = [(v.get("x", 0), v.get("y", 0)) for v in vertices]
        x, y = _polygon_centroid(points)
        lines.append({"text": annotation.get("description", ""), "x": x, "y": y})
    return lines


async def call_provider(
    image_bytes: bytes, *, provider: str, endpoint: str, api_key: str
) -> list[dict]:
    """Call the configured OCR provider and return a normalised line list.

    Raises OcrProviderError for any network failure, timeout, non-2xx response, or
    response shape this module cannot parse - never returns a partial result.
    """
    if provider == "azure":
        if not endpoint:
            raise OcrProviderError("OCR_ENDPOINT is not configured for the Azure provider.")
        url = f"{endpoint.rstrip('/')}{_AZURE_ANALYZE_PATH}"
        headers = {
            "Ocp-Apim-Subscription-Key": api_key,
            "Content-Type": "application/octet-stream",
        }
        try:
            async with httpx.AsyncClient(timeout=OCR_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    url,
                    params={"features": "read", "api-version": _AZURE_API_VERSION},
                    headers=headers,
                    content=image_bytes,
                )
        except httpx.TimeoutException as exc:
            raise OcrProviderError("Timed out waiting for the Azure OCR provider.") from exc
        except httpx.HTTPError as exc:
            raise OcrProviderError(f"Could not reach the Azure OCR provider: {exc}") from exc

        if response.status_code >= 400:
            raise OcrProviderError(f"Azure OCR provider returned HTTP {response.status_code}.")
        try:
            data = response.json()
        except ValueError as exc:
            raise OcrProviderError("Azure OCR provider returned malformed JSON.") from exc
        return normalize_azure_response(data)

    if provider == "google":
        body = {
            "requests": [
                {
                    "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                    "features": [{"type": "TEXT_DETECTION"}],
                }
            ]
        }
        try:
            async with httpx.AsyncClient(timeout=OCR_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    _GOOGLE_ANNOTATE_URL, params={"key": api_key}, json=body
                )
        except httpx.TimeoutException as exc:
            raise OcrProviderError("Timed out waiting for the Google OCR provider.") from exc
        except httpx.HTTPError as exc:
            raise OcrProviderError(f"Could not reach the Google OCR provider: {exc}") from exc

        if response.status_code >= 400:
            raise OcrProviderError(f"Google OCR provider returned HTTP {response.status_code}.")
        try:
            data = response.json()
        except ValueError as exc:
            raise OcrProviderError("Google OCR provider returned malformed JSON.") from exc
        return normalize_google_response(data)

    raise OcrProviderError(
        f"'{provider}' is not a lines-based OCR provider. Must be one of ('azure', 'google') - "
        "'anthropic' is handled separately (see call_anthropic_provider) and 'tesseract' never "
        "reaches this function at all (it runs client-side - see diagnose_scanned_label)."
    )


async def call_anthropic_provider(image_bytes: bytes, *, api_key: str) -> dict:
    """The strongest optional cloud fallback (off by default - OCR_PROVIDER must be
    set to "anthropic" explicitly): sends the cropped label image to the Messages
    API with positional instructions a traditional OCR engine can't take (which
    corner the line label is in, which line to ignore) and asks for strict JSON
    back, bypassing the lines/parse_label pipeline entirely - see diagnose_label.
    """
    if not api_key:
        raise OcrProviderError("OCR_API_KEY is not configured for the Anthropic provider.")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": _ANTHROPIC_MODEL,
        "max_tokens": 512,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": _ANTHROPIC_PROMPT},
                ],
            }
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=OCR_TIMEOUT_SECONDS) as client:
            response = await client.post(_ANTHROPIC_MESSAGES_URL, headers=headers, json=body)
    except httpx.TimeoutException as exc:
        raise OcrProviderError("Timed out waiting for the Anthropic OCR provider.") from exc
    except httpx.HTTPError as exc:
        raise OcrProviderError(f"Could not reach the Anthropic OCR provider: {exc}") from exc

    if response.status_code >= 400:
        raise OcrProviderError(f"Anthropic OCR provider returned HTTP {response.status_code}.")
    try:
        data = response.json()
        text = data["content"][0]["text"]
        fields = json.loads(text)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise OcrProviderError(
            "Anthropic OCR provider returned a response that wasn't the strict JSON asked for."
        ) from exc
    if not isinstance(fields, dict):
        raise OcrProviderError("Anthropic OCR provider's JSON response was not an object.")
    return fields


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------
#
# Everything below operates on the normalised line list only - it never knows or
# cares which provider produced it.

# Order/qty/ship, e.g. "#178414 [22] S". The lenient fallback patterns below run
# only when this strict, all-in-one pattern doesn't match anywhere in the joined
# text (e.g. the OCR engine split the run across two separate lines).
_ORDER_QTY_SHIP_RE = re.compile(r"#?\s*(\d{6})\s*[\[\(]\s*(\d+)\s*[\]\)]\s*([DSP])\b")
_ORDER_HASH_RE = re.compile(r"#\s*(\d{6})\b")
_ORDER_BARE_RE = re.compile(r"\b(\d{6})\b")
_QTY_BRACKET_RE = re.compile(r"[\[\(]\s*(\d+)\s*[\]\)]")
# Fallback only - not in the original spec pattern, added because the strict
# pattern's ship-code group can fail to match (e.g. a stray character between the
# bracket and the letter) even when the order/qty half matched fine. A bare,
# word-bounded D/S/P is ambiguous with a line-label candidate of the same letter in
# principle, but ship code and order/qty are read from the SAME text run here, so
# this fallback only fires against the joined line text, never against the
# line-label candidate search below.
_SHIP_CODE_FALLBACK_RE = re.compile(r"\b([DSP])\b")

# Lines containing any of these (case-insensitive substring match) describe a part
# of the drawer OTHER than the drawer box itself (the bottom panel, internal
# dividers, hardware inserts, finish/sanding shorthand) - excluded from dimension
# and thickness matching so e.g. "Bot: 1/4  10.625 x 20.438" never becomes the
# drawer's own dimensions or side thickness.
_DECOY_LINE_KEYWORDS: tuple[str, ...] = (
    "bot",
    "ears",
    "lips",
    "rem div",
    "inserts",
    "fh:",
    "ss:",
)

_DIMENSIONS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)")

_SPECIES_KEYWORDS: tuple[str, ...] = ("maple", "oak", "walnut", "cherry", "birch")
_VALID_THICKNESS_FRACTIONS: frozenset[str] = frozenset({"1/2", "5/8", "3/8", "1/4", "3/4"})
_FRACTION_RE = re.compile(r"\b(\d/\d)\b")

# The separator is a pipe on the physical label, but OCR frequently misreads a
# thin vertical stroke as a capital I, lowercase l/L, or an exclamation mark.
_CORNER_BLOCK_RE = re.compile(r"(\d/\d)\s*[|IlL!]\s*(\d+(?:\.\d+)?)")

# Line-label candidates: 1-2 uppercase letters once separator punctuation is
# stripped. `^[A-Z]{1,2}$` already excludes every 3+ letter decoy token (MSK, PRE,
# MDF, BOT) on length alone; SS/FH/WW are excluded explicitly since they DO fit
# that length. Do NOT add single letters here - D/S/P are valid line labels on a
# long order and are told apart from the ship code by geometry only (see
# find_line_label below), never by a word list.
_LINE_LABEL_CANDIDATE_RE = re.compile(r"^[A-Z]{1,2}$")
_LINE_LABEL_STRIP_CHARS = ".:|,()[]"
_LINE_LABEL_BLOCKLIST: frozenset[str] = frozenset({"SS", "FH", "MSK", "PRE", "MDF", "WW", "BOT"})


def _is_decoy_line(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in _DECOY_LINE_KEYWORDS)


def parse_order_qty_ship(lines: list[dict]) -> dict:
    """Order number, quantity, and ship code - read together since they appear in
    the same text run on the label. Returns {"order_number", "quantity",
    "ship_code"}, any of which may be None if nothing matched."""
    joined = " ".join(line.get("text", "") for line in lines)

    match = _ORDER_QTY_SHIP_RE.search(joined)
    if match:
        return {
            "order_number": match.group(1),
            "quantity": int(match.group(2)),
            "ship_code": match.group(3),
        }

    order_match = _ORDER_HASH_RE.search(joined) or _ORDER_BARE_RE.search(joined)
    qty_match = _QTY_BRACKET_RE.search(joined)
    ship_match = _SHIP_CODE_FALLBACK_RE.search(joined)
    return {
        "order_number": order_match.group(1) if order_match else None,
        "quantity": int(qty_match.group(1)) if qty_match else None,
        "ship_code": ship_match.group(1) if ship_match else None,
    }


def parse_dimensions(lines: list[dict]) -> dict | None:
    """Height x Width x Depth from the species line - the first non-decoy line
    matching the pattern. None if no non-decoy line matches at all."""
    for line in lines:
        text = line.get("text", "")
        if _is_decoy_line(text):
            continue
        match = _DIMENSIONS_RE.search(text)
        if match:
            return {
                "height": float(match.group(1)),
                "width": float(match.group(2)),
                "depth": float(match.group(3)),
            }
    return None


def _line_fraction(text: str) -> str | None:
    match = _FRACTION_RE.search(text)
    if match and match.group(1) in _VALID_THICKNESS_FRACTIONS:
        return match.group(1)
    return None


def parse_thickness(lines: list[dict]) -> str | None:
    """Side thickness: preferentially a fraction on a line naming a species
    (maple/oak/walnut/cherry/birch), falling back to the first valid fraction on
    any other non-decoy line. Decoy lines (e.g. the bottom panel's own "Bot: 1/4")
    are excluded from both passes - a fallback that picked up the bottom panel's
    thickness as the drawer's own would be exactly the kind of decoy mismatch this
    function exists to avoid."""
    for line in lines:
        text = line.get("text", "")
        if _is_decoy_line(text):
            continue
        if any(species in text.lower() for species in _SPECIES_KEYWORDS):
            fraction = _line_fraction(text)
            if fraction:
                return fraction

    for line in lines:
        text = line.get("text", "")
        if _is_decoy_line(text):
            continue
        fraction = _line_fraction(text)
        if fraction:
            return fraction
    return None


def parse_corner_block(lines: list[dict]) -> dict | None:
    """The corner block restates thickness and height, e.g. "1/2 | 7". Searched
    per-line first (the common case - it's its own short line), then against the
    full joined text as a fallback in case OCR split it mid-token."""
    for line in lines:
        match = _CORNER_BLOCK_RE.search(line.get("text", ""))
        if match:
            return {"thickness": match.group(1), "height": float(match.group(2))}

    joined = " ".join(line.get("text", "") for line in lines)
    match = _CORNER_BLOCK_RE.search(joined)
    if match:
        return {"thickness": match.group(1), "height": float(match.group(2))}
    return None


def _strip_line_label_token(text: str) -> str:
    return text.strip().strip(_LINE_LABEL_STRIP_CHARS).upper()


def find_line_label_candidates(lines: list[dict]) -> list[dict]:
    """Every OCR line whose (stripped, uppercased) text is 1-2 letters and not on
    the blocklist - a candidate set to be ranked by geometry, not narrowed by
    content alone (see parse_line_label)."""
    candidates: list[dict] = []
    for line in lines:
        token = _strip_line_label_token(line.get("text", ""))
        if not token or not _LINE_LABEL_CANDIDATE_RE.match(token):
            continue
        if token in _LINE_LABEL_BLOCKLIST:
            continue
        candidates.append({"text": token, "x": line["x"], "y": line["y"]})
    return candidates


def _squared_distance(ax: float, ay: float, bx: float, by: float) -> float:
    return (ax - bx) ** 2 + (ay - by) ** 2


def parse_line_label(lines: list[dict], *, qr_x: float | None, qr_y: float | None) -> dict:
    """The line label sits diagonally opposite the QR code; the ship code sits
    right beside it. Ranking every 1-2 letter candidate by squared distance from
    the QR's position, furthest first, is what separates a line label from a
    ship-code letter of the same value (e.g. "D" as both) - never a word list,
    since D/S/P are all valid line labels too.

    Falls back to distance from the centroid of every OCR line when the browser
    didn't supply a QR position (`used_centroid_fallback` records that this
    happened, so a caller can tell a low-confidence read from a normal one).

    Returns {"value": str | None, "alternates": list[str], "used_centroid_fallback": bool}.
    Alternates are the next-furthest candidates (up to 3) - required so a wrong
    answer's near-misses are visible, not just the one field parse_label() commits to.
    """
    candidates = find_line_label_candidates(lines)
    if not candidates:
        return {"value": None, "alternates": [], "used_centroid_fallback": False}

    used_centroid_fallback = qr_x is None or qr_y is None
    if used_centroid_fallback:
        ref_x = sum(line["x"] for line in lines) / len(lines)
        ref_y = sum(line["y"] for line in lines) / len(lines)
    else:
        ref_x, ref_y = qr_x, qr_y

    ranked = sorted(
        candidates,
        key=lambda c: _squared_distance(c["x"], c["y"], ref_x, ref_y),
        reverse=True,
    )
    return {
        "value": ranked[0]["text"],
        "alternates": [c["text"] for c in ranked[1:4]],
        "used_centroid_fallback": used_centroid_fallback,
    }


@dataclass
class ParsedLabel:
    order_number: str | None
    quantity: int | None
    ship_code: str | None
    line_label: str | None
    line_label_alternates: list[str]
    line_label_used_centroid_fallback: bool
    dimensions: dict | None
    thickness: str | None
    corner_block: dict | None


def parse_label(
    lines: list[dict], *, qr_x: float | None = None, qr_y: float | None = None
) -> ParsedLabel:
    """Run every field parser above against one normalised line list. Any field
    may come back None - that is a normal, expected outcome for a label read under
    real shop-floor conditions, not an error; see validate_parsed_label for how a
    missing field is reported (skipped, never silently treated as a pass)."""
    order_qty_ship = parse_order_qty_ship(lines)
    line_label = parse_line_label(lines, qr_x=qr_x, qr_y=qr_y)
    return ParsedLabel(
        order_number=order_qty_ship["order_number"],
        quantity=order_qty_ship["quantity"],
        ship_code=order_qty_ship["ship_code"],
        line_label=line_label["value"],
        line_label_alternates=line_label["alternates"],
        line_label_used_centroid_fallback=line_label["used_centroid_fallback"],
        dimensions=parse_dimensions(lines),
        thickness=parse_thickness(lines),
        corner_block=parse_corner_block(lines),
    )


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

#: Corner-block height is read off the same physical label as the dimension line,
#: so an exact float comparison would be too strict against real OCR digit noise
#: (e.g. "7" vs "7.0") - a small tolerance, not a rounding rule.
CORNER_HEIGHT_TOLERANCE = 0.001


@dataclass
class ValidationResult:
    failures: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def validate_parsed_label(parsed: ParsedLabel, *, qr_order_number: str | None) -> ValidationResult:
    """Three cross-checks. A check that could not run because a needed field never
    parsed is recorded as SKIPPED, never as a silent pass - conflating the two
    would make a label the parser mostly failed to read look identical to one that
    genuinely checked out."""
    result = ValidationResult()

    if qr_order_number is None or parsed.order_number is None:
        result.skipped.append("order_number_matches_qr")
    elif parsed.order_number != qr_order_number:
        result.failures.append(
            f"OCR'd order number '{parsed.order_number}' does not match the QR "
            f"code's order number '{qr_order_number}'."
        )

    if parsed.corner_block is None or parsed.dimensions is None:
        result.skipped.append("corner_block_height_matches_dimensions")
    elif abs(parsed.corner_block["height"] - parsed.dimensions["height"]) > CORNER_HEIGHT_TOLERANCE:
        result.failures.append(
            f"Corner block height {parsed.corner_block['height']} does not match "
            f"the dimension line's first figure {parsed.dimensions['height']}."
        )

    if parsed.corner_block is None or parsed.thickness is None:
        result.skipped.append("corner_block_thickness_matches_species_line")
    elif parsed.corner_block["thickness"] != parsed.thickness:
        result.failures.append(
            f"Corner block thickness '{parsed.corner_block['thickness']}' does not "
            f"match the species line's thickness '{parsed.thickness}'."
        )

    return result


def _line_label_discarded(parsed: ParsedLabel, qr_order_number: str | None) -> bool:
    """PROJECT_SPEC_PHASE9.md "Validate against the QR": an OCR'd order number that
    disagrees with the QR's decoded order number makes the WHOLE read untrustworthy,
    not just that one field - most likely the crop geometry latched onto the wrong
    label (e.g. a neighbouring drawer's label in frame) or OCR hallucinated
    something plausible-looking. Never shown to the operator; see diagnose_label /
    diagnose_scanned_label, which both null out line_label/line_label_alternates
    when this is True. Deliberately the same "both present and different" test
    validate_parsed_label already uses for its own order_number_matches_qr check -
    one definition of "mismatch", not two."""
    return (
        qr_order_number is not None
        and parsed.order_number is not None
        and parsed.order_number != qr_order_number
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _build_scan_result(
    parsed: ParsedLabel,
    *,
    qr_order_number: str | None,
    raw_lines: list[dict],
    elapsed_ms: float,
) -> dict:
    """Shared by both entry points below - parse_label() has already run, this
    just validates and shapes the plain dict ScanDiagnosticOut expects."""
    validation = validate_parsed_label(parsed, qr_order_number=qr_order_number)
    discarded = _line_label_discarded(parsed, qr_order_number)
    return {
        "order_number": parsed.order_number,
        "quantity": parsed.quantity,
        "ship_code": parsed.ship_code,
        "line_label": None if discarded else parsed.line_label,
        "line_label_alternates": [] if discarded else parsed.line_label_alternates,
        "line_label_used_centroid_fallback": parsed.line_label_used_centroid_fallback,
        "line_label_discarded": discarded,
        "dimensions": parsed.dimensions,
        "thickness": parsed.thickness,
        "corner_block": parsed.corner_block,
        "validator_failures": validation.failures,
        "validator_skipped": validation.skipped,
        "raw_lines": raw_lines,
        "elapsed_ms": elapsed_ms,
    }


async def diagnose_label(
    image_bytes: bytes,
    *,
    provider: str,
    endpoint: str,
    api_key: str,
    qr_order_number: str | None,
    qr_x: float | None,
    qr_y: float | None,
) -> dict:
    """The optional cloud-provider path (azure/google/anthropic - POST
    /api/v1/scan/diagnose, off by default). Calls the provider, parses,
    validates, and returns one plain dict ready for the router to hand back as
    JSON. Writes nothing anywhere - no DB session parameter exists on this
    function on purpose.

    Raises ServiceError (app/errors.py) - not OcrProviderError - on any provider
    failure, so app/main.py's existing exception handler turns it into the app's
    normal {"error": {"message", "field"}} envelope. This is the one place this
    module's internal error type crosses into the app-wide hierarchy.
    """
    started = time.monotonic()
    if provider == "anthropic":
        try:
            fields = await call_anthropic_provider(image_bytes, api_key=api_key)
        except OcrProviderError as exc:
            raise ServiceError(str(exc)) from exc
        parsed = ParsedLabel(
            order_number=fields.get("order_number"),
            quantity=fields.get("quantity"),
            ship_code=fields.get("ship_code"),
            line_label=fields.get("line_label"),
            # A single vision-language-model read has no "next-furthest candidate"
            # concept the way geometry-ranked OCR lines do - no alternates, and
            # never the degraded centroid-fallback path (there's no QR position
            # dependency here at all).
            line_label_alternates=[],
            line_label_used_centroid_fallback=False,
            dimensions=fields.get("dimensions"),
            thickness=fields.get("thickness"),
            corner_block=fields.get("corner_block"),
        )
        raw_lines: list[dict] = []
    else:
        try:
            raw_lines = await call_provider(
                image_bytes, provider=provider, endpoint=endpoint, api_key=api_key
            )
        except OcrProviderError as exc:
            raise ServiceError(str(exc)) from exc
        parsed = parse_label(raw_lines, qr_x=qr_x, qr_y=qr_y)
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)

    return _build_scan_result(
        parsed, qr_order_number=qr_order_number, raw_lines=raw_lines, elapsed_ms=elapsed_ms
    )


def diagnose_scanned_label(
    lines: list[dict],
    *,
    qr_order_number: str | None,
    qr_x: float | None,
    qr_y: float | None,
) -> dict:
    """The default, browser-side path (POST /api/v1/scan/parse-label - see
    app/routers/scan.py and app/static/js/label-scan.js). `lines` are raw text
    lines Tesseract.js already recognised client-side, from the line-label and
    dimension crops it derived from the QR's geometry, each translated back into
    the photo's original coordinate space so they rank against qr_x/qr_y exactly
    like a cloud provider's lines already do (parse_line_label).

    Synchronous and network-free on purpose: this never calls a provider, so it
    needs no OCR_API_KEY and keeps working even when every cloud provider is
    unconfigured - the recognition already happened on the phone; this is pure
    text parsing + the same cross-field validation diagnose_label() uses, one
    implementation, never duplicated in JavaScript (PROJECT_SPEC_PHASE9.md Part 3).
    """
    parsed = parse_label(lines, qr_x=qr_x, qr_y=qr_y)
    return _build_scan_result(
        parsed, qr_order_number=qr_order_number, raw_lines=lines, elapsed_ms=0.0
    )
