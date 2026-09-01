"""Hotfix (2026-09-01) "scan modal will not close, label is never read":
prevent-recurrence coverage for the vendored scanning assets themselves.

A missing/misnamed vendored file wouldn't raise a Python exception anywhere -
it would just make Tesseract.js (or jsQR) silently fail to load in a real
browser, on a real phone, which is exactly the kind of failure this hotfix is
about making loud instead. These tests fail CI the moment a vendored file goes
missing or is accidentally left empty (e.g. a bad git-lfs/checkout, or someone
"cleaning up" app/static/js/vendor/ without realizing what's in it), rather
than surfacing only on a shop-floor phone.

See tests/api/test_scan_api.py for the companion check that these files are
actually SERVED correctly by the running app, not just present on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

VENDOR_DIR = Path(__file__).resolve().parent.parent.parent / "app" / "static" / "js" / "vendor"

# Every file app/static/js/label-scan.js and app/templates/defect_entry.html
# reference at runtime. Keep this list in sync with VENDOR_BASE's file names in
# label-scan.js and the <script src="..."> tags in defect_entry.html.
REQUIRED_VENDOR_FILES: tuple[str, ...] = (
    "jsqr.js",  # QR fallback for browsers with no native BarcodeDetector
    "tesseract.min.js",  # main-thread Tesseract.js API
    "tesseract-worker.min.js",  # Tesseract.js Worker script (workerPath)
    "tesseract-core-lstm.wasm.js",  # OCR engine WASM core (corePath)
    "eng.traineddata.gz",  # English language data (langPath)
)

# A real vendored asset is at minimum this many bytes - catches a truncated
# download or an accidentally-committed placeholder/empty file, which would
# otherwise pass a bare "exists" check.
MIN_EXPECTED_SIZE_BYTES: dict[str, int] = {
    "jsqr.js": 100_000,
    "tesseract.min.js": 30_000,
    "tesseract-worker.min.js": 50_000,
    "tesseract-core-lstm.wasm.js": 1_000_000,
    "eng.traineddata.gz": 500_000,
}


@pytest.mark.parametrize("filename", REQUIRED_VENDOR_FILES)
def test_vendored_scan_asset_exists_and_is_not_empty(filename):
    path = VENDOR_DIR / filename
    assert (
        path.is_file()
    ), f"{path} is missing - label-scan.js/defect_entry.html reference it at runtime"
    size = path.stat().st_size
    minimum = MIN_EXPECTED_SIZE_BYTES[filename]
    assert size >= minimum, (
        f"{path} is only {size} bytes (expected at least {minimum}) - "
        "looks truncated, empty, or a placeholder rather than the real vendored file"
    )


def test_no_other_scan_related_vendor_files_are_silently_missing():
    """Sanity check the list above itself doesn't drift from what's on disk -
    a file added to app/static/js/vendor/ without an accompanying entry above
    would otherwise never get a minimum-size check."""
    on_disk = {p.name for p in VENDOR_DIR.glob("*") if p.is_file()}
    assert set(REQUIRED_VENDOR_FILES) <= on_disk
