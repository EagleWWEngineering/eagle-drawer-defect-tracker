"""Phase 2: single shared login for the whole app.

There are no per-user accounts and no roles here - one username/password pair
(APP_USERNAME / APP_PASSWORD_HASH, read from the environment / .env) gates the
entire app so it isn't sitting open on the LAN with zero protection. The existing
"Role (prototype)" selector in the header (app/dependencies.py get_actor_role) is a
completely separate, cosmetic, unauthenticated label used only for the audit log -
it is NOT touched by this module and must not be confused with real auth.

Sessions are rows in the `auth_sessions` table (see app/models.py AuthSession), not
a signed/stateless token with a built-in expiry - that's what makes "log out
everywhere" possible (delete every row) and makes a normal "log out" only affect one
device (delete just that row). A session is valid for as long as its row exists,
with no time-based expiry check anywhere in this module.

Credentials are read directly from the environment on every call (not through
app.config.get_settings(), which is @lru_cache'd process-wide) so tests can set
APP_USERNAME/APP_PASSWORD_HASH per-test with monkeypatch.setenv without fighting a
cached Settings singleton.
"""

from __future__ import annotations

import datetime as dt
import os
import secrets

import bcrypt
from sqlalchemy.orm import Session

from app.models import AuthSession

# Long-lived, HttpOnly cookie. Plain HTTP on the LAN (no reverse-proxy TLS), so the
# Secure flag is deliberately NOT set - it would prevent the browser from ever
# sending the cookie back and lock everyone out. max_age is a very long, but finite,
# number (browsers require *some* value for a persistent cookie) - the actual
# "never expires" guarantee comes from the server never checking the age of the
# corresponding auth_sessions row, not from this cookie lifetime.
SESSION_COOKIE_NAME = "eagle_session"
SESSION_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 365 * 10  # 10 years


def get_app_username() -> str:
    return os.getenv("APP_USERNAME", "")


def get_app_password_hash() -> str:
    return os.getenv("APP_PASSWORD_HASH", "")


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


def verify_credentials(username: str, password: str) -> bool:
    """True only if both the username and password match the one configured
    shared login. Uses a constant-time comparison for the username too, same as
    bcrypt.checkpw already does internally for the password digest."""
    expected_username = get_app_username()
    if not expected_username or not username:
        return False
    if not secrets.compare_digest(username, expected_username):
        return False
    return _verify_password(password, get_app_password_hash())


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
