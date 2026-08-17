"""Phase 2: single shared login for the whole app.

There are no per-user accounts and no roles here - one username/password pair
gates the entire app so it isn't sitting open on the LAN with zero protection. The
existing "Role (prototype)" selector in the header (app/dependencies.py
get_actor_role) is a completely separate, cosmetic, unauthenticated label used only
for the audit log - it is NOT touched by this module and must not be confused with
real auth.

Sessions are rows in the `auth_sessions` table (see app/models.py AuthSession), not
a signed/stateless token with a built-in expiry - that's what makes "log out
everywhere" possible (delete every row) and makes a normal "log out" only affect one
device (delete just that row). A session is valid for as long as its row exists,
with no time-based expiry check anywhere in this module.

Credential storage (post-incident redesign): APP_USERNAME / APP_PASSWORD_HASH start
life as environment variables (.env locally, Render dashboard env vars in
production), but the value actually checked against at login time lives in the
`app_settings` table (see app/models.py AppSetting), under the
AUTH_USERNAME_SETTING_KEY / AUTH_PASSWORD_HASH_SETTING_KEY rows. `sync_credentials_
from_env()` below copies the environment into those rows and is called from
app/seed_data.py seed_master_data() on *every* app startup (not only against an
empty database) - so changing the env var in Render's dashboard and letting it
redeploy/restart always takes effect. The original version of this module read
os.getenv(...) directly at check time with no DB storage at all, which was correct
in principle but turned out to be fragile in practice: a stray trailing newline or
space picked up when pasting a bcrypt hash into Render's dashboard silently made
every login attempt fail (bcrypt.checkpw raises ValueError on a malformed hash,
caught below and treated the same as "wrong password" - see _verify_password).
sync_credentials_from_env() strips both values before storing them specifically to
close that hole.
"""

from __future__ import annotations

import datetime as dt
import os
import secrets

import bcrypt
from sqlalchemy.orm import Session

from app.models import AppSetting, AuthSession

AUTH_USERNAME_SETTING_KEY = "auth_username"
AUTH_PASSWORD_HASH_SETTING_KEY = "auth_password_hash"

# Long-lived, HttpOnly cookie. Plain HTTP on the LAN (no reverse-proxy TLS), so the
# Secure flag is deliberately NOT set - it would prevent the browser from ever
# sending the cookie back and lock everyone out. max_age is a very long, but finite,
# number (browsers require *some* value for a persistent cookie) - the actual
# "never expires" guarantee comes from the server never checking the age of the
# corresponding auth_sessions row, not from this cookie lifetime.
SESSION_COOKIE_NAME = "eagle_session"
SESSION_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 365 * 10  # 10 years


def _clean_env_value(raw: str) -> str:
    """Strip whitespace, then a single matching pair of surrounding quotes.

    Render's dashboard (and most other host env-var UIs) stores whatever is typed
    completely literally - there is no shell or dotenv-style parsing. Typing
    `APP_PASSWORD_HASH="$2b$12$..."` the way you would in a shell script or a
    quoted .env example stores the quote characters as part of the value.
    python-dotenv strips quotes like this automatically when parsing a local
    .env file, which is exactly why this was easy to miss locally and only bite
    in production."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value


def sync_credentials_from_env(db: Session) -> None:
    """Copy APP_USERNAME / APP_PASSWORD_HASH from the environment into the
    app_settings table, creating the rows if missing or updating them if the
    environment has changed since the last startup. Called from
    app/seed_data.py seed_master_data() on every app startup (idempotent - a
    no-op once the stored values already match).

    Does not touch auth_sessions - changing the credential does not force any
    existing session to re-authenticate. Only "Log out everywhere" does that.

    Values are cleaned (whitespace, then a matching pair of surrounding quotes)
    before being stored - see _clean_env_value and the module docstring for why
    that specifically matters here.
    """
    env_username = _clean_env_value(os.getenv("APP_USERNAME", ""))
    env_password_hash = _clean_env_value(os.getenv("APP_PASSWORD_HASH", ""))

    username_setting = db.get(AppSetting, AUTH_USERNAME_SETTING_KEY)
    if username_setting is None:
        db.add(AppSetting(key=AUTH_USERNAME_SETTING_KEY, value=env_username))
    elif username_setting.value != env_username:
        username_setting.value = env_username

    hash_setting = db.get(AppSetting, AUTH_PASSWORD_HASH_SETTING_KEY)
    if hash_setting is None:
        db.add(AppSetting(key=AUTH_PASSWORD_HASH_SETTING_KEY, value=env_password_hash))
    elif hash_setting.value != env_password_hash:
        hash_setting.value = env_password_hash
    # Caller (seed_master_data) commits - left uncommitted here so it composes
    # into the same startup transaction as the rest of master-data seeding.


def get_app_username(db: Session) -> str:
    setting = db.get(AppSetting, AUTH_USERNAME_SETTING_KEY)
    return setting.value if setting is not None else ""


def get_app_password_hash(db: Session) -> str:
    setting = db.get(AppSetting, AUTH_PASSWORD_HASH_SETTING_KEY)
    return setting.value if setting is not None else ""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed/empty hash (e.g. APP_PASSWORD_HASH unset in a fresh checkout).
        return False


def verify_credentials(db: Session, username: str, password: str) -> bool:
    """True only if both the username and password match the one configured
    shared login. Uses a constant-time comparison for the username too, same as
    bcrypt.checkpw already does internally for the password digest."""
    expected_username = get_app_username(db)
    if not expected_username or not username:
        return False
    if not secrets.compare_digest(username, expected_username):
        return False
    return _verify_password(password, get_app_password_hash(db))


def create_session(db: Session) -> str:
    """Create a new, never-expiring session row and return its token."""
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(token=token))
    db.commit()
    return token


def get_session(db: Session, token: str | None) -> AuthSession | None:
    """Look up a session by its cookie token. No expiry check by design - see the
    module docstring. Returns None for a missing/blank/unknown token."""
    if not token:
        return None
    return db.query(AuthSession).filter(AuthSession.token == token).first()


def delete_session(db: Session, token: str | None) -> None:
    """Normal 'Log out': end just this one session, leaving every other active
    session (other devices/browsers) untouched."""
    if not token:
        return
    db.query(AuthSession).filter(AuthSession.token == token).delete()
    db.commit()


def delete_all_sessions(db: Session) -> int:
    """'Log out everywhere': invalidate every currently active session at once.
    Returns the number of sessions that were deleted."""
    count = db.query(AuthSession).delete()
    db.commit()
    return count


def session_age_seconds(session: AuthSession, *, now: dt.datetime | None = None) -> float:
    """Purely informational (e.g. for a future 'active since' display) - never used
    to decide whether a session is still valid. Exists mainly so a test can prove a
    very old row is still accepted without the module having any actual expiry
    branch to point at."""
    now = now or dt.datetime.now(dt.timezone.utc)
    created_at = session.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.timezone.utc)
    return (now - created_at).total_seconds()
