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
_LINE_LABEL_BLOCKLIST: frozenset[str] = frozenset(
    {"SS", "FH", "MSK", "PRE", "MDF", "WW", "BOT", "X"}
)

# PROJECT_SPEC_PHASE9.md "select by corner proximity, then fall back to a
# picker" fix: on real labels, the corner block ("5/8 | 6") sits immediately
# beside the line-label letter, on the SAME LINE - a whole-label OCR pass can
# merge them into one token ("3.5 B", "N+B") that the isolated-token match
# above rejects outright, so the true letter never even becomes a candidate.
# These extract a 1-2 letter run from the very START or END of a token
# (never the middle - "WW-2606" must never become "WW" by stripping interior
# digits) for tokens that survive the corner-proximity gate below.
_TRAILING_LETTERS_RE = re.compile(r"([A-Z]{1,2})$")
_LEADING_LETTERS_RE = re.compile(r"^([A-Z]{1,2})")

#: How many multiples of the QR's own size a candidate may sit from the
#: expected corner and still qualify - "does not need to be precise" (the
#: corner estimate itself has some error), but text from elsewhere on the
#: label (a different word entirely) must not qualify just because nothing
#: better was found. First-pass value, not calibrated against a real label.
CORNER_DISTANCE_CAP_QR_MULTIPLE = 2.5

#: Tesseract confidence (0-100) a candidate must clear to be trusted. Chosen
#: deliberately on the strict side: a wrong line label silently written to
#: the database is worse than a blank one (the whole point of this field is
#: being able to trust it), so this fix would rather show the manual letter
#: picker (see app/static/js/label-scan.js) than guess. A candidate with no
#: confidence value at all (e.g. a hypothetical future source that doesn't
#: report one) is treated as unverified, not rejected - never blocking on
#: data the caller didn't provide.
LINE_LABEL_MIN_CONFIDENCE = 65.0

#: Two candidates with the same extracted letter within this many multiples
#: of the QR's size are the same physical letter, seen twice - once by the
#: block-of-text pass, once by the sparse-text pass over the same corner
#: (see app/static/js/label-scan.js) - not two different letters.
CANDIDATE_DEDUPE_DISTANCE_QR_MULTIPLE = 0.5


def _extract_line_label_candidates_from_token(raw_text: str) -> list[str]:
    """A token that survived intact as an isolated 1-2 letter word is handled
    by the exact-match path in find_line_label_candidates already; this is
    the fallback for a token OCR merged with neighbouring punctuation/digits
    (e.g. "3.5B", "|3.5B", "N+B") - looks only at the token's own leading and
    trailing run of letters, NEVER removes characters from the middle, so a
    token like "WW-2606" doesn't spuriously yield "WW" by stripping its
    interior digits. Deduplicates leading==trailing (the isolated-token case,
    e.g. "A" alone, matches both ends identically)."""
    cleaned = raw_text.strip().strip(_LINE_LABEL_STRIP_CHARS).upper()
    found: list[str] = []
    trailing = _TRAILING_LETTERS_RE.search(cleaned)
    if trailing and trailing.group(1) not in found:
        found.append(trailing.group(1))
    leading = _LEADING_LETTERS_RE.search(cleaned)
    if leading and leading.group(1) not in found:
        found.append(leading.group(1))
    return [text for text in found if text not in _LINE_LABEL_BLOCKLIST]


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


def find_line_label_candidates(
    lines: list[dict],
    *,
    corner_x: float | None = None,
    corner_y: float | None = None,
    max_distance: float | None = None,
) -> list[dict]:
    """Every OCR line/word that could plausibly BE the line label - a candidate
    set to be ranked/filtered by geometry and confidence, not narrowed by
    content alone (see parse_line_label). Two ways a token qualifies:

      1. Isolated: the whole (stripped, uppercased) token is 1-2 letters and
         not on the blocklist - attempted for EVERY token regardless of
         position (unchanged from before this fix; the distance cap below is
         what actually decides whether a far-away isolated match survives).
      2. Merged: for tokens within `max_distance` of (corner_x, corner_y)
         ONLY, a 1-2 letter run extracted from the token's own leading/
         trailing edge (see _extract_line_label_candidates_from_token) -
         PROJECT_SPEC_PHASE9.md "select by corner proximity" fix. Scoped to
         near-corner tokens on purpose: extracting letter fragments from
         every multi-word phrase on the label (species names, customer name,
         ...) would flood the candidate pool for no reason.

    `corner_x`/`corner_y`/`max_distance` are all optional - when any is
    omitted, only the isolated-token path runs and every candidate's
    "distance"/"confidence" come back None (the cloud-provider path's
    original behaviour, byte-for-byte - see parse_line_label).
    """
    gated_by_corner = corner_x is not None and corner_y is not None and max_distance is not None
    candidates: list[dict] = []
    for line in lines:
        raw_text = line.get("text", "")
        distance = (
            _squared_distance(line["x"], line["y"], corner_x, corner_y) ** 0.5
            if gated_by_corner
            else None
        )

        isolated = _strip_line_label_token(raw_text)
        if (
            isolated
            and _LINE_LABEL_CANDIDATE_RE.match(isolated)
            and isolated not in _LINE_LABEL_BLOCKLIST
        ):
            texts = [isolated]
        elif gated_by_corner and distance is not None and distance <= max_distance:
            texts = _extract_line_label_candidates_from_token(raw_text)
        else:
            texts = []

        for text in texts:
            candidates.append(
                {
                    "text": text,
                    "x": line["x"],
                    "y": line["y"],
                    "confidence": line.get("confidence"),
                    "distance": distance,
                }
            )
    return candidates


def _dedupe_candidates_by_position(candidates: list[dict], *, threshold: float) -> list[dict]:
    """Two candidates with the same extracted text within `threshold` pixels
    of each other are the same physical letter seen twice - once by the
    block-of-text pass, once by the sparse-text pass over the same corner
    (PROJECT_SPEC_PHASE9.md "sparse-text recognition pass" fix). Keeps
    whichever occurrence has the higher confidence (first-seen on a tie)."""
    kept: list[dict] = []
    for candidate in candidates:
        duplicate_index = next(
            (
                i
                for i, existing in enumerate(kept)
                if existing["text"] == candidate["text"]
                and _squared_distance(existing["x"], existing["y"], candidate["x"], candidate["y"])
                ** 0.5
                <= threshold
            ),
            None,
        )
        if duplicate_index is None:
            kept.append(candidate)
            continue
        existing = kept[duplicate_index]
        existing_confidence = existing["confidence"] if existing["confidence"] is not None else -1.0
        new_confidence = candidate["confidence"] if candidate["confidence"] is not None else -1.0
        if new_confidence > existing_confidence:
            kept[duplicate_index] = candidate
    return kept


def _squared_distance(ax: float, ay: float, bx: float, by: float) -> float:
    return (ax - bx) ** 2 + (ay - by) ** 2


def _rank_by_expected_corner(
    candidates: list[dict], *, corner_x: float, corner_y: float, qr_size: float
) -> dict:
    """PROJECT_SPEC_PHASE9.md "select by corner proximity, then fall back to a
    picker": rank every candidate by distance TO the expected corner
    (nearest wins) instead of distance FROM the QR (furthest wins) - these are
    NOT equivalent. "Furthest from the QR" is satisfied by anything at the
    far end of the label, including text along an edge that isn't the corner
    at all; "nearest the expected corner" is satisfied only by what's
    actually there.

    A candidate beyond CORNER_DISTANCE_CAP_QR_MULTIPLE * qr_size, or below
    LINE_LABEL_MIN_CONFIDENCE, is NOT a candidate at all - if nothing
    qualifies, the line genuinely could not be read (value=None). A wrong
    line label silently written to the database is worse than a blank one,
    so this never reaches for a distant token just because it's the closest
    thing available; see app/static/js/label-scan.js for the manual letter
    picker this is designed to hand off to on failure.

    Every candidate (qualifying or not) comes back in `ranked_candidates`,
    annotated with why it was rejected (if it was) - `?scandebug=1` shows
    this so a bad read is diagnosable, not just invisible.
    """
    deduped = _dedupe_candidates_by_position(
        candidates, threshold=qr_size * CANDIDATE_DEDUPE_DISTANCE_QR_MULTIPLE
    )
    max_distance = qr_size * CORNER_DISTANCE_CAP_QR_MULTIPLE

    annotated = []
    for c in deduped:
        if c["distance"] is None or c["distance"] > max_distance:
            reason = "distance"
        elif c["confidence"] is not None and c["confidence"] < LINE_LABEL_MIN_CONFIDENCE:
            reason = "confidence"
        else:
            reason = None
        annotated.append({**c, "rejected_reason": reason})
    annotated.sort(key=lambda c: c["distance"] if c["distance"] is not None else float("inf"))

    qualifying = [c for c in annotated if c["rejected_reason"] is None]
    return {
        "value": qualifying[0]["text"] if qualifying else None,
        "alternates": [c["text"] for c in qualifying[1:4]],
        "used_centroid_fallback": False,
        "ranked_candidates": annotated,
    }


def parse_line_label(
    lines: list[dict],
    *,
    qr_x: float | None,
    qr_y: float | None,
    corner_x: float | None = None,
    corner_y: float | None = None,
    qr_size: float | None = None,
) -> dict:
    """When `corner_x`/`corner_y`/`qr_size` are all supplied (the default
    browser-side Tesseract path - see app/static/js/label-scan.js), candidates
    are ranked by distance to the expected corner (nearest wins), with a
    distance cap and confidence floor both enforced - see
    _rank_by_expected_corner. This is the path real labels need: the label's
    own corner-block text ("5/8 | 6") sits right beside the line label, so
    OCR frequently merges them into one token and/or fragments neighbouring
    words into stray isolated letters elsewhere on the label - ranking by
    proximity to a known expected point, with a hard cap, is what keeps a
    distant fragment from winning just because nothing better showed up.

    Otherwise (the optional cloud providers, which don't compute this
    geometry) falls back to the ORIGINAL ranking: every 1-2 letter candidate
    by squared distance from the QR's position, furthest first - the line
    label sits diagonally opposite the QR, the ship code right beside it, so
    "furthest from the QR" told them apart there. Falls back further still to
    distance from the centroid of every OCR line when even the QR position is
    missing (`used_centroid_fallback` records that this happened).

    Returns {"value": str | None, "alternates": list[str],
    "used_centroid_fallback": bool, "ranked_candidates": list[dict]}.
    Alternates are the next-best candidates (up to 3) that themselves qualify
    (never a rejected filler) - required so a wrong answer's near-misses are
    visible as one-tap corrections, not just the one field parse_label()
    commits to. `ranked_candidates` is every candidate found (qualifying or
    not, each with its own distance and rejection reason if any) -
    PROJECT_SPEC_PHASE9.md fix: exposed so `?scandebug=1` can show the real
    decision, not a recomputed guess.
    """
    use_corner_ranking = corner_x is not None and corner_y is not None and qr_size is not None
    max_distance = qr_size * CORNER_DISTANCE_CAP_QR_MULTIPLE if use_corner_ranking else None

    candidates = find_line_label_candidates(
        lines,
        corner_x=corner_x if use_corner_ranking else None,
        corner_y=corner_y if use_corner_ranking else None,
        max_distance=max_distance,
    )
    if not candidates:
        return {
            "value": None,
            "alternates": [],
            "used_centroid_fallback": False,
            "ranked_candidates": [],
        }

    if use_corner_ranking:
        return _rank_by_expected_corner(
            candidates, corner_x=corner_x, corner_y=corner_y, qr_size=qr_size
        )

    # --- Original furthest-from-QR ranking (cloud providers) - unchanged ---
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
    ranked_candidates = [
        {
            "text": c["text"],
            "x": c["x"],
            "y": c["y"],
            "distance": _squared_distance(c["x"], c["y"], ref_x, ref_y) ** 0.5,
            "confidence": c.get("confidence"),
            "rejected_reason": None,  # this ranking path has no cap/floor to reject against
        }
        for c in ranked
    ]
    return {
        "value": ranked[0]["text"],
        "alternates": [c["text"] for c in ranked[1:4]],
        "used_centroid_fallback": used_centroid_fallback,
        "ranked_candidates": ranked_candidates,
    }


@dataclass
class ParsedLabel:
    order_number: str | None
    quantity: int | None
    ship_code: str | None
    line_label: str | None
    line_label_alternates: list[str]
    line_label_used_centroid_fallback: bool
    line_label_candidates: list[dict]
    dimensions: dict | None
    thickness: str | None
    corner_block: dict | None


def parse_label(
    lines: list[dict],
    *,
    qr_x: float | None = None,
    qr_y: float | None = None,
    corner_x: float | None = None,
    corner_y: float | None = None,
    qr_size: float | None = None,
) -> ParsedLabel:
    """Run every field parser above against one normalised line list. Any field
    may come back None - that is a normal, expected outcome for a label read under
    real shop-floor conditions, not an error; see validate_parsed_label for how a
    missing field is reported (skipped, never silently treated as a pass).

    `lines` may be WORD-level or LINE-level entries - this function (and every
    parser it calls) only ever looks at {"text", "x", "y"} (plus an optional
    "confidence" - see parse_line_label), so it makes no difference which
    granularity produced them. The default browser-side path sends word-level
    entries from a whole-label pass plus a sparse-text pass over the expected
    corner (see app/static/js/label-scan.js); the optional cloud providers
    still send line-level entries with no confidence and no corner_x/corner_y/
    qr_size - one implementation either way, see parse_line_label for how the
    two paths' ranking differs.

    `corner_x`/`corner_y`/`qr_size` are the browser's own estimate of where
    the line label should be and the QR's size (the distance-cap reference
    unit) - see parse_line_label."""
    order_qty_ship = parse_order_qty_ship(lines)
    line_label = parse_line_label(
        lines, qr_x=qr_x, qr_y=qr_y, corner_x=corner_x, corner_y=corner_y, qr_size=qr_size
    )
    return ParsedLabel(
        order_number=order_qty_ship["order_number"],
        quantity=order_qty_ship["quantity"],
        ship_code=order_qty_ship["ship_code"],
        line_label=line_label["value"],
        line_label_alternates=line_label["alternates"],
        line_label_used_centroid_fallback=line_label["used_centroid_fallback"],
        line_label_candidates=line_label["ranked_candidates"],
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

#: PROJECT_SPEC_PHASE9.md hotfix (2026-09-01, "line label never fills"): how many
#: digits may differ between the OCR'd order number and the QR's before a
#: disagreement is treated as GENUINE rather than plausible OCR noise - see
#: _order_numbers_confidently_disagree.
ORDER_NUMBER_MAX_TOLERATED_DIGIT_DIFFERENCES = 1


@dataclass
class ValidationResult:
    failures: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _order_numbers_confidently_disagree(ocr_value: str, qr_value: str) -> bool:
    """True only for a genuine, confident disagreement between the OCR'd order
    number and the QR's decoded one - not a single-digit OCR slip.

    Both values are always exactly 6 digits when this is even called (see
    _ORDER_QTY_SHIP_RE / _ORDER_HASH_RE / _ORDER_BARE_RE - parsed.order_number is
    never anything else), so a plain per-position comparison is enough; no need
    for a general edit-distance algorithm.

    Found in production (PROJECT_SPEC_PHASE9.md hotfix, 2026-09-01, "line label
    never fills"): the dimension crop sits right beside the QR - the same area
    the order number is itself printed - so a slightly-off crop can pick up a
    stray digit or two from the real order number, forming a 6-digit run that
    differs from the QR's by only one digit. Discarding an otherwise-good line
    label read over that single misread digit throws away good data for no
    reason; a difference of two or more digits is no longer plausibly noise and
    still discards (see _line_label_discarded)."""
    if len(ocr_value) != len(qr_value):
        return True
    differences = sum(1 for a, b in zip(ocr_value, qr_value, strict=True) if a != b)
    return differences > ORDER_NUMBER_MAX_TOLERATED_DIGIT_DIFFERENCES


def validate_parsed_label(parsed: ParsedLabel, *, qr_order_number: str | None) -> ValidationResult:
    """Three cross-checks. A check that could not run because a needed field never
    parsed is recorded as SKIPPED, never as a silent pass - conflating the two
    would make a label the parser mostly failed to read look identical to one that
    genuinely checked out. A one-digit order-number disagreement is ALSO recorded
    as skipped, not failed - see _order_numbers_confidently_disagree: it's treated
    as unverified rather than a confident mismatch."""
    result = ValidationResult()

    if qr_order_number is None or parsed.order_number is None:
        result.skipped.append("order_number_matches_qr")
    elif parsed.order_number == qr_order_number:
        pass
    elif _order_numbers_confidently_disagree(parsed.order_number, qr_order_number):
        result.failures.append(
            f"OCR'd order number '{parsed.order_number}' does not match the QR "
            f"code's order number '{qr_order_number}'."
        )
    else:
        result.skipped.append("order_number_matches_qr")

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
    CONFIDENTLY disagrees with the QR's decoded order number makes the WHOLE read
    untrustworthy, not just that one field - most likely the crop geometry latched
    onto the wrong label (e.g. a neighbouring drawer's label in frame) or OCR
    hallucinated something plausible-looking. Never shown to the operator; see
    diagnose_label / diagnose_scanned_label, which both null out line_label/
    line_label_alternates when this is True. Deliberately the same confident-
    disagreement test validate_parsed_label uses for its own
    order_number_matches_qr check (see _order_numbers_confidently_disagree) - one
    definition of "mismatch", not two, and a one-digit OCR slip does not count -
    only a genuine disagreement discards an otherwise-good line-label read."""
    return (
        qr_order_number is not None
        and parsed.order_number is not None
        and _order_numbers_confidently_disagree(parsed.order_number, qr_order_number)
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
        # Diagnostic only - never gated by `discarded` (unlike line_label/
        # alternates above, which the operator could act on) - ?scandebug=1
        # shows the full ranking a real word-level pass produced regardless of
        # whether the read was ultimately trusted (PROJECT_SPEC_PHASE9.md
        # "read the whole label" fix).
        "line_label_candidates": parsed.line_label_candidates,
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
            line_label_candidates=[],
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
    corner_x: float | None = None,
    corner_y: float | None = None,
    qr_size: float | None = None,
) -> dict:
    """The default, browser-side path (POST /api/v1/scan/parse-label - see
    app/routers/scan.py and app/static/js/label-scan.js). `lines` are raw text
    entries Tesseract.js already recognised client-side - word-level, from a
    whole-label pass and a sparse-text pass over the expected line-label
    corner, plus the dimension crop's own line-level entries - each translated
    back into the photo's original coordinate space so they rank against
    qr_x/qr_y (and corner_x/corner_y) exactly like a cloud provider's lines
    already do (parse_line_label).

    `corner_x`/`corner_y`/`qr_size` are the browser's own estimate of the
    expected line-label corner and the QR's size - when supplied, the line
    label is ranked by proximity to that point with a distance cap and
    confidence floor instead of the older furthest-from-QR ranking (see
    parse_line_label / _rank_by_expected_corner).

    Synchronous and network-free on purpose: this never calls a provider, so it
    needs no OCR_API_KEY and keeps working even when every cloud provider is
    unconfigured - the recognition already happened on the phone; this is pure
    text parsing + the same cross-field validation diagnose_label() uses, one
    implementation, never duplicated in JavaScript (PROJECT_SPEC_PHASE9.md Part 3).
    """
    parsed = parse_label(
        lines, qr_x=qr_x, qr_y=qr_y, corner_x=corner_x, corner_y=corner_y, qr_size=qr_size
    )
    return _build_scan_result(
        parsed, qr_order_number=qr_order_number, raw_lines=lines, elapsed_ms=0.0
    )
