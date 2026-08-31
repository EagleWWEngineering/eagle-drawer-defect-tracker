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
    # Phase 3 relay ingest: an unattended local script (scripts/relay_customer_
    # issues.py) can't hold a browser session. Protected instead by its own
    # RELAY_API_KEY header check inside the route itself (app/routers/sync.py
    # _verify_relay_key) - same security bar as the login, different credential.
    # Exempting this ONE exact path only - nothing broader.
    "/api/v1/sync/customer-issues/ingest-raw",
    # Same reasoning: the local relay's frequent (~1 minute) heartbeat check-in
    # (scripts/relay_poll.py) is also an unattended script that can't hold a
    # browser session - protected by the same RELAY_API_KEY header check instead
    # (see _verify_relay_key). Exempting this ONE exact path only - nothing
    # broader. Note GET .../relay-connection (what the logged-in browser UI polls
    # for its status line) is a DIFFERENT path and stays behind the normal login.
    "/api/v1/sync/customer-issues/relay-status",
    # Phase 6: same reasoning again - the relay's independent second forward
    # (scraped drawers-scheduled figures) is also an unattended script call,
    # protected by the same RELAY_API_KEY header check (_verify_relay_key).
    # Exempting this ONE exact path only - nothing broader.
    "/api/v1/sync/daily-schedule/ingest-raw",
    # Brief Export (Part A): the Eagle production brief's VM fetches this daily
    # (~06:15 ET) to build its drawers TV board - also an unattended,
    # machine-to-machine caller with no browser session, protected instead by its
    # own X-Brief-Key header check (app/routers/brief.py _verify_brief_key), same
    # discipline as the relay paths above but a separate key (BRIEF_API_KEY) since
    # this is a different machine, calling in the opposite direction. Exempting
    # this ONE exact path only - nothing broader.
    "/api/v1/brief/summary",
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
