"""Phase 2: login / logout / logout-everywhere for the single shared login.

See app/services/auth_service.py for the session model and app/auth_middleware.py
for how every other route is protected. There is only one username/password for the
whole app - no roles, no per-user accounts.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.errors import ValidationError
from app.services import auth_service

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


class LogoutEverywhereIn(BaseModel):
    # Re-entering the current password is required to confirm this destructive,
    # all-devices action (PROJECT_SPEC.md Phase 2 addendum).
    password: str


class LogoutEverywhereOut(BaseModel):
    ok: bool
    sessions_invalidated: int


class OkOut(BaseModel):
    ok: bool


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        auth_service.SESSION_COOKIE_NAME,
        token,
        max_age=auth_service.SESSION_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=False,  # plain HTTP on the LAN - Secure would block the cookie entirely
        samesite="lax",
        path="/",
    )


@router.post("/login", response_model=OkOut)
def login(payload: LoginIn, response: Response, db: Session = Depends(get_db)) -> OkOut:
    if not auth_service.verify_credentials(payload.username, payload.password):
        raise ValidationError("Incorrect username or password.", field="password")
    token = auth_service.create_session(db)
    _set_session_cookie(response, token)
    return OkOut(ok=True)


@router.post("/logout", response_model=OkOut)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> OkOut:
    """Ends only THIS device/browser's session - every other active session (other
    devices) is left untouched."""
    token = request.cookies.get(auth_service.SESSION_COOKIE_NAME)
    auth_service.delete_session(db, token)
    response.delete_cookie(auth_service.SESSION_COOKIE_NAME, path="/")
    return OkOut(ok=True)


@router.post("/logout-everywhere", response_model=LogoutEverywhereOut)
def logout_everywhere(
    payload: LogoutEverywhereIn, response: Response, db: Session = Depends(get_db)
) -> LogoutEverywhereOut:
    """Invalidates every currently active session at once (all devices/browsers),
    including this one - the caller must re-authenticate afterward. Requires
    re-entering the current shared password to confirm (this middleware already
    guarantees the caller is currently logged in on at least this device)."""
    if not auth_service.verify_credentials(auth_service.get_app_username(), payload.password):
        raise ValidationError("Incorrect password.", field="password")
    count = auth_service.delete_all_sessions(db)
    response.delete_cookie(auth_service.SESSION_COOKIE_NAME, path="/")
    return LogoutEverywhereOut(ok=True, sessions_invalidated=count)
