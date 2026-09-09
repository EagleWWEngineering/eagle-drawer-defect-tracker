"""Prevent-recurrence coverage for the vendored QR-scanning asset (jsQR).

A missing/misnamed vendored file wouldn't raise a Python exception anywhere -
it would just make jsQR silently fail to load in a real browser, on a real
phone. This test fails CI the moment the vendored file goes missing or is
accidentally left empty (e.g. a bad git-lfs/checkout, or someone "cleaning up"
app/static/js/vendor/ without realizing what's in it), rather than surfacing
only on a shop-floor phone.

See tests/api/test_vendor_assets_api.py for the companion check that this file
is actually SERVED correctly by the running app, not just present on disk.

PHASE 9 REMOVAL (2026-09-09): the Tesseract.js OCR engine and its vendored
assets (tesseract.min.js, tesseract-worker.min.js, tesseract-core-lstm.wasm.js,
eng.traineddata.gz) have been removed entirely - see
app/static/js/label-scan.js's module docstring. Only jsQR (QR decoding, which
still works) remains vendored.
"""

from __future__ import annotations

from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parent.parent.parent / "app" / "static" / "js" / "vendor"

# A real vendored jsQR build is at minimum this many bytes - catches a
# truncated download or an accidentally-committed placeholder/empty file,
# which would otherwise pass a bare "exists" check.
MIN_EXPECTED_SIZE_BYTES = 100_000


def test_jsqr_vendor_asset_exists_and_is_not_empty():
    path = VENDOR_DIR / "jsqr.js"
    assert path.is_file(), f"{path} is missing - label-scan.js references it at runtime"
    size = path.stat().st_size
    assert size >= MIN_EXPECTED_SIZE_BYTES, (
        f"{path} is only {size} bytes (expected at least {MIN_EXPECTED_SIZE_BYTES}) - "
        "looks truncated, empty, or a placeholder rather than the real vendored file"
    )


def test_no_ocr_vendor_files_survive_removal():
    """PROJECT_SPEC_PHASE9.md Part 1: the Tesseract OCR path was removed
    entirely, including its vendored assets - this fails if any of them
    reappear (e.g. a bad merge/rebase resurrecting a deleted file)."""
    on_disk = {p.name for p in VENDOR_DIR.glob("*") if p.is_file()}
    removed = {
        "tesseract.min.js",
        "tesseract-worker.min.js",
        "tesseract-core-lstm.wasm.js",
        "eng.traineddata.gz",
    }
    assert not (removed & on_disk), f"OCR vendor files should be gone: {removed & on_disk}"
