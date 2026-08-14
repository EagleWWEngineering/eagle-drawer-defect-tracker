"""Phase 2: gate every route behind the single shared login except an explicit
small allowlist (health check, the login page/endpoint itself, and static assets
needed to render the login page).

Implemented as ASGI middleware (rather than a per-route `Depends(...)`) so "protect
every route" is enforced in one place and a new page/router can never accidentally
forget to add the dependency. It resolves the DB session through
`request.app.dependency_overrides` (falling back to the real `get_db`) instead of
importing `SessionLocal` directly, so tests that override `get_db` with an isolated
in-memory database are respected here too - this middleware must never touch the
real data/defect_tracker.db file during a test run.
"""

from __future__ import annotations

from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from app.dependencies import get_db
from app.services import auth_service

# Exact paths reachable with no session at all.
PUBLIC_EXACT_PATHS: set[str] = {
    "/login",
    "/api/v1/health",
    "/api/v1/auth/login",
}

# Path prefixes reachable with no session (static assets only - no shop data lives
# under /static, unlike /uploads which DOES contain real defect photos and is
# therefore intentionally NOT in this allowlist).
PUBLIC_PATH_PREFIXES: tuple[str, ...] = ("/static/",)


def _is_public(path: str) -> bool:
    return path in PUBLIC_EXACT_PATHS or path.startswith(PUBLIC_PATH_PREFIXES)


class LoginRequiredMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_public(path):
            return await call_next(request)

        token = request.cookies.get(auth_service.SESSION_COOKIE_NAME)
        db_dependency = request.app.dependency_overrides.get(get_db, get_db)
        db_gen = db_dependency()
        db = next(db_gen)
        try:
            session = auth_service.get_session(db, token)
        finally:
            db_gen.close()

        if session is None:
            if path.startswith("/api/"):
                return JSONResponse(
                    status_code=401,
                    content={"error": {"message": "Login required.", "field": None}},
                )
            next_qs = f"?next={quote(path)}" if path not in ("/", "") else ""
            return RedirectResponse(url=f"/login{next_qs}", status_code=303)

        return await call_next(request)
