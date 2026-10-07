"""Eagle-vm migration (2026-10).

- ROOT_PATH: the app served under production count's /defects/* pass-through.
  Routes stay at "/"; only generated links, redirects, cookies and photo URLs
  carry the prefix.
- GET /api/v1/sync/export/{database,uploads}: one-off export off Render,
  protected by X-Relay-Key.
"""

from __future__ import annotations

import io
import sqlite3
import tarfile

import pytest

from app.config import _normalize_root_path, get_settings
from app.main import templates
from app.schemas import DefectPhotoOut
from app.services import auth_service

TEST_RELAY_KEY = "test-relay-key-do-not-use-in-prod"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("/", ""),
        ("defects", "/defects"),
        ("/defects/", "/defects"),
        (" /defects ", "/defects"),
    ],
)
def test_root_path_is_normalized(raw, expected):
    assert _normalize_root_path(raw) == expected


@pytest.fixture()
def prefixed(monkeypatch):
    monkeypatch.setenv("ROOT_PATH", "/defects")
    get_settings.cache_clear()
    monkeypatch.setitem(templates.env.globals, "base", "/defects")
    yield "/defects"
    get_settings.cache_clear()


def test_pages_link_under_the_prefix(client, prefixed):
    html = client.get("/").text
    assert 'window.APP_BASE = "/defects";' in html
    assert 'href="/defects/defect-entry"' in html
    assert 'src="/defects/static/js/api.js' in html
    assert 'href="/defect-entry"' not in html


def test_pages_link_at_the_root_without_a_prefix(client):
    html = client.get("/").text
    assert 'window.APP_BASE = "";' in html
    assert 'href="/defect-entry"' in html


def test_login_redirect_carries_the_prefix(client, prefixed):
    client.cookies.clear()
    resp = client.get("/reports", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/defects/login?next=/reports"


def test_session_cookie_is_scoped_to_the_prefix(client, prefixed, monkeypatch):
    monkeypatch.setattr(auth_service, "verify_credentials", lambda *_a, **_k: True)
    resp = client.post("/api/v1/auth/login", json={"username": "u", "password": "p"})
    assert resp.status_code == 200
    assert "Path=/defects" in resp.headers["set-cookie"]


def test_photo_url_carries_the_prefix(prefixed):
    photo = DefectPhotoOut(
        id=1,
        original_filename="a.jpg",
        content_type="image/jpeg",
        uploaded_at="2026-10-06T12:00:00",
        stored_filename="abc.jpg",
    )
    assert photo.url == "/defects/uploads/abc.jpg"


@pytest.fixture()
def export_env(monkeypatch, tmp_path):
    db_file = tmp_path / "live.db"
    con = sqlite3.connect(db_file)
    con.execute("CREATE TABLE marker (v TEXT)")
    con.execute("INSERT INTO marker VALUES ('from-render')")
    con.commit()
    con.close()
    uploads = tmp_path / "uploads"
    (uploads / "sub").mkdir(parents=True)
    (uploads / "one.jpg").write_bytes(b"jpeg-one")
    (uploads / "sub" / "two.jpg").write_bytes(b"jpeg-two")
    monkeypatch.setenv("RELAY_API_KEY", TEST_RELAY_KEY)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("UPLOADS_DIR", str(uploads))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.mark.parametrize("path", ["/api/v1/sync/export/database", "/api/v1/sync/export/uploads"])
def test_export_requires_the_relay_key(client, export_env, path):
    client.cookies.clear()  # reachable without a login session...
    assert client.get(path).status_code == 401  # ...but never without the key
    assert client.get(path, headers={"X-Relay-Key": "wrong"}).status_code == 401


def test_export_database_is_a_consistent_copy(client, export_env):
    resp = client.get("/api/v1/sync/export/database", headers={"X-Relay-Key": TEST_RELAY_KEY})
    assert resp.status_code == 200
    copy = export_env / "copy.db"
    copy.write_bytes(resp.content)
    con = sqlite3.connect(copy)
    assert con.execute("SELECT v FROM marker").fetchone()[0] == "from-render"
    con.close()


def test_export_uploads_contains_every_photo(client, export_env):
    resp = client.get("/api/v1/sync/export/uploads", headers={"X-Relay-Key": TEST_RELAY_KEY})
    assert resp.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        names = sorted(tar.getnames())
        assert names == ["one.jpg", "sub/two.jpg"]
        assert tar.extractfile("sub/two.jpg").read() == b"jpeg-two"


@pytest.fixture()
def no_login(monkeypatch):
    monkeypatch.setenv("LOGIN_REQUIRED", "false")
    get_settings.cache_clear()
    monkeypatch.setitem(templates.env.globals, "login_required", False)
    yield
    get_settings.cache_clear()


def test_login_off_opens_pages_and_api_without_a_session(client, no_login):
    client.cookies.clear()
    assert client.get("/reports", follow_redirects=False).status_code == 200
    assert client.get("/api/v1/rework-queue").status_code == 200


def test_login_off_sends_the_login_page_home(client, no_login, prefixed):
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/defects/"


def test_login_off_hides_the_settings_link(client, no_login):
    assert 'href="/settings"' not in client.get("/").text
    assert "There is nothing to log out of" in client.get("/settings").text


def test_login_off_still_guards_the_machine_endpoints_with_their_keys(client, no_login, export_env):
    client.cookies.clear()
    assert client.get("/api/v1/sync/export/database").status_code == 401


def test_login_stays_on_by_default(client):
    client.cookies.clear()
    assert client.get("/reports", follow_redirects=False).status_code == 303
    assert 'href="/settings"' not in client.get("/login").text  # login page has no nav


def test_scanner_page_url_renders_into_new_defect(client):
    html = client.get("/defect-entry").text
    assert "https://eaglewwengineering.github.io/eagle-label-scanner/" in html
