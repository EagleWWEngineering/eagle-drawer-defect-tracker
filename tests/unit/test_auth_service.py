"""Unit tests for app/services/auth_service.py credential sync (Phase 5 incident
fix). See tests/api/test_auth_api.py for the end-to-end login/session tests.

Regression coverage for the bug this fixes: APP_USERNAME/APP_PASSWORD_HASH used to
be read directly from the environment with no stored copy at all, which sounds
stateless-safe but in practice meant nothing ever proved the env var Render
actually handed the process matched what was typed into its dashboard. These tests
exercise the DB-backed sync directly, independent of the HTTP layer.
"""

from __future__ import annotations

from app.models import AppSetting
from app.services import auth_service


def test_sync_creates_rows_from_env_on_first_call(db_session, monkeypatch):
    # db_session's seed_master_data() already called sync once with whatever the
    # ambient environment was; overwrite and re-sync to get a known starting point.
    monkeypatch.setenv("APP_USERNAME", "shop")
    monkeypatch.setenv("APP_PASSWORD_HASH", "fake-hash-1")
    auth_service.sync_credentials_from_env(db_session)
    db_session.commit()

    assert auth_service.get_app_username(db_session) == "shop"
    assert auth_service.get_app_password_hash(db_session) == "fake-hash-1"


def test_sync_updates_an_already_stored_credential(db_session, monkeypatch):
    """The exact scenario that was broken: a credential already exists in the
    database (from a previous startup), the environment variable changes, and a
    later sync (simulating a restart) must overwrite the stored value - not leave
    the original in place forever."""
    monkeypatch.setenv("APP_USERNAME", "shop")
    monkeypatch.setenv("APP_PASSWORD_HASH", "fake-hash-1")
    auth_service.sync_credentials_from_env(db_session)
    db_session.commit()
    assert auth_service.get_app_password_hash(db_session) == "fake-hash-1"

    # Env var changes (e.g. Rodolfo generates a new hash and updates Render's
    # dashboard), then the app restarts - sync runs again.
    monkeypatch.setenv("APP_PASSWORD_HASH", "fake-hash-2")
    auth_service.sync_credentials_from_env(db_session)
    db_session.commit()

    assert auth_service.get_app_password_hash(db_session) == "fake-hash-2"


def test_sync_strips_whitespace_from_env_values(db_session, monkeypatch):
    """A stray trailing newline/space from copy-pasting a hash into Render's
    dashboard used to silently break every login (bcrypt.checkpw treats a
    malformed hash the same as a wrong password). Sync must not store it."""
    monkeypatch.setenv("APP_USERNAME", "  shop\n")
    monkeypatch.setenv("APP_PASSWORD_HASH", "fake-hash-1\n")
    auth_service.sync_credentials_from_env(db_session)
    db_session.commit()

    assert auth_service.get_app_username(db_session) == "shop"
    assert auth_service.get_app_password_hash(db_session) == "fake-hash-1"


def test_sync_strips_surrounding_quotes_from_env_values(db_session, monkeypatch):
    """Render's dashboard (and most host env-var UIs) stores values completely
    literally - typing APP_PASSWORD_HASH="$2b$12$..." the way you would in a
    shell script or a quoted .env example stores the quote characters as part of
    the value. python-dotenv strips this automatically for a local .env file,
    which is exactly why this was easy to miss locally."""
    monkeypatch.setenv("APP_USERNAME", '"shop"')
    monkeypatch.setenv("APP_PASSWORD_HASH", "'fake-hash-1'")
    auth_service.sync_credentials_from_env(db_session)
    db_session.commit()

    assert auth_service.get_app_username(db_session) == "shop"
    assert auth_service.get_app_password_hash(db_session) == "fake-hash-1"


def test_sync_leaves_a_lone_quote_character_alone(db_session, monkeypatch):
    """Only strip a matching pair of surrounding quotes - never touch a quote
    character that's genuinely part of the value (e.g. a mismatched pair, or a
    single stray character), so a real credential containing a quote can't be
    silently mangled."""
    monkeypatch.setenv("APP_USERNAME", "o'brien")
    monkeypatch.setenv("APP_PASSWORD_HASH", "fake-hash-1")
    auth_service.sync_credentials_from_env(db_session)
    db_session.commit()

    assert auth_service.get_app_username(db_session) == "o'brien"


def test_sync_is_a_no_op_when_env_already_matches_stored_value(db_session, monkeypatch):
    monkeypatch.setenv("APP_USERNAME", "shop")
    monkeypatch.setenv("APP_PASSWORD_HASH", "fake-hash-1")
    auth_service.sync_credentials_from_env(db_session)
    db_session.commit()
    hash_key = auth_service.AUTH_PASSWORD_HASH_SETTING_KEY
    updated_at_first = db_session.get(AppSetting, hash_key).updated_at

    auth_service.sync_credentials_from_env(db_session)
    db_session.commit()
    updated_at_second = db_session.get(AppSetting, hash_key).updated_at

    assert updated_at_first == updated_at_second


def test_sync_does_not_touch_existing_sessions(db_session, monkeypatch):
    """Changing the credential must not force-invalidate active sessions - only
    an explicit 'Log out everywhere' does that (app/routers/auth.py)."""
    token = auth_service.create_session(db_session)

    monkeypatch.setenv("APP_USERNAME", "shop")
    monkeypatch.setenv("APP_PASSWORD_HASH", "a-brand-new-hash")
    auth_service.sync_credentials_from_env(db_session)
    db_session.commit()

    assert auth_service.get_session(db_session, token) is not None
