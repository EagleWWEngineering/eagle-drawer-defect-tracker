"""PROJECT_SPEC_PHASE9.md Part 2: the A-Z line letter picker is a permanent
part of the New Defect form (not a conditional/OCR-triggered popup), and the
OCR-era line-label messaging it replaced must not survive anywhere in the
template or the scan JS.

The picker itself is plain client-side JS with no browser test harness in
this repo (see app/static/js/label-scan.js's own module docstring - "no JS
framework exists or was added"), so this asserts on the shipped template/JS
source directly - the same style of prevent-recurrence check
tests/unit/test_scan_vendor_assets.py already uses for the vendored files.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFECT_ENTRY_HTML = (PROJECT_ROOT / "app" / "templates" / "defect_entry.html").read_text(
    encoding="utf-8"
)
LABEL_SCAN_JS = (PROJECT_ROOT / "app" / "static" / "js" / "label-scan.js").read_text(
    encoding="utf-8"
)

# Phrases from the OCR-era Line field messaging (discarded reads, low-confidence
# guesses, a couldn't-read-it hint) that must never survive anywhere in the
# template - the picker replaces all of them with one neutral, always-shown hint.
_OCR_ERA_LINE_PHRASES: tuple[str, ...] = (
    "didn't match this label's QR code",
    "line read was",
    "discarded",
    "Couldn't read the line",
    "couldn't be read",
)


def test_neutral_line_helper_text_is_present():
    assert "Tap the work order line, or type it." in DEFECT_ENTRY_HTML


def test_no_ocr_era_line_messaging_survives_in_the_template():
    for phrase in _OCR_ERA_LINE_PHRASES:
        assert phrase not in DEFECT_ENTRY_HTML, f"OCR-era line messaging still present: {phrase!r}"


def test_scandebug_mode_is_removed():
    assert "scandebug" not in DEFECT_ENTRY_HTML
    assert "scandebug" not in LABEL_SCAN_JS


def test_line_label_picker_is_a_permanent_element_not_conditional():
    """The picker's container must be unconditionally in the markup (no
    `style="display:none"` gate that only a scan-complete callback lifts) -
    the whole point of Part 2 is that it's always visible, scan or no scan."""
    assert 'id="line-label-picker"' in DEFECT_ENTRY_HTML
    # The old conditional popup's now-removed sub-elements must not reappear.
    for removed_id in (
        "line-label-picker-singles",
        "line-label-picker-two-letter-toggle",
        "line-label-picker-first",
        "line-label-picker-second",
    ):
        assert (
            removed_id not in DEFECT_ENTRY_HTML
        ), f"old conditional picker element survived: {removed_id!r}"


def test_label_scan_js_has_no_ocr_remnants():
    """The module docstring's own removal note legitimately mentions
    "Tesseract" once, by name, to explain what used to be here (matching this
    repo's convention of documenting removed code in place - see e.g.
    app/config.py's history comments) - what must actually be gone is any
    CODE that still uses it."""
    forbidden_snippets = (
        "window.Tesseract",
        "OCR_PROVIDER",
        "parse-label",
        "diagnose",
        "PSM_",
        "createTesseractWorker",
    )
    for forbidden in forbidden_snippets:
        assert (
            forbidden not in LABEL_SCAN_JS
        ), f"OCR remnant still present in label-scan.js: {forbidden!r}"


def test_ocr_vendor_script_tag_is_removed_from_template():
    assert "tesseract" not in DEFECT_ENTRY_HTML.lower()
