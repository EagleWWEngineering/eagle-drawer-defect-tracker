"""New Defect form: "Save with photo" + equal-size disposition buttons.

No JS test framework exists in this repo (see test_line_label_picker.py), so
these pin the wiring that matters in the template/scripts themselves; the
end-to-end flow (camera -> save -> upload) was verified in a real browser.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
HTML = (PROJECT_ROOT / "app" / "templates" / "defect_entry.html").read_text(encoding="utf-8")
API_JS = (PROJECT_ROOT / "app" / "static" / "js" / "api.js").read_text(encoding="utf-8")
CSS = (PROJECT_ROOT / "app" / "static" / "css" / "app.css").read_text(encoding="utf-8")


def test_save_with_photo_opens_the_rear_camera():
    assert 'id="save-with-photo-btn"' in HTML
    start = HTML.index('id="save-photo-input"')
    tag = HTML[HTML.rindex("<input", 0, start) : HTML.index(">", start)]
    assert 'type="file"' in tag
    assert 'capture="environment"' in tag
    # Only types the server accepts (app/routers/defect_cases.py ALLOWED_PHOTO_TYPES).
    assert 'accept="image/jpeg,image/png,image/webp"' in tag


def test_camera_opens_from_the_tap_after_validation_and_saves_on_photo():
    handler = HTML[HTML.index('saveWithPhotoBtn.addEventListener("click"') :]
    handler = handler[: handler.index("});")]
    assert "validateBeforeSubmit(form)" in handler
    assert "savePhotoInput.click()" in handler
    # Nothing async before the click, or phones block the camera.
    assert "await" not in handler
    assert 'savePhotoInput.addEventListener("change"' in HTML
    assert "saveCase(file)" in HTML


def test_save_without_photo_still_exists():
    assert 'id="save-btn"' in HTML
    assert "Save without photo" in HTML
    assert "saveCase(null)" in HTML


def test_case_is_created_before_photo_upload():
    body = HTML[HTML.index("async function saveCase(photoFile)") :]
    assert body.index("Api.createDefectCase") < body.index("Api.uploadPhoto(created.id")


def test_every_upload_is_shrunk_first():
    upload = API_JS[API_JS.index("uploadPhoto: async") :]
    assert "await shrinkPhotoForUpload(file)" in upload[: upload.index("},")]
    assert "PHOTO_MAX_DIMENSION = 1600" in API_JS


def test_disposition_buttons_are_equal_size():
    assert 'id="disposition-buttons"' in HTML
    assert 'dispositionButtonHtml(DISPOSITION_PRIMARY, "tap-btn-disposition")' in HTML
    assert 'dispositionButtonHtml(DISPOSITION_SECONDARY, "tap-btn-disposition")' in HTML
    assert "grid-template-columns: 1fr 1fr" in CSS[CSS.index(".disposition-grid") :][:200]


def test_session_log_photo_is_one_tap_camera_upload():
    cell = HTML[HTML.index("function sessionPhotoCellHtml") :]
    cell = cell[: cell.index("function addSessionLogRow")]
    assert 'capture="environment"' in cell
    assert "📷 Add photo" in cell
    assert "+ Add another" in cell
    assert "📷 attached" in cell
    # The old two-step "choose file, then Attach" button is gone.
    assert ">Attach<" not in HTML
    click = HTML[HTML.index('sessionLogTable.addEventListener("click"') :]
    click = click[: click.index("});")]
    assert "input.click()" in click
    assert "await" not in click
    assert 'sessionLogTable.addEventListener("change"' in HTML
