# Phase 5 Addendum — Authentication (single shared login)

Addendum to [`PROJECT_SPEC.md`](PROJECT_SPEC.md) and the Phase 2/3/4 addenda.
Everything in all of them still applies unchanged. This document covers only what
Phase 5 added: a single shared login that gates the entire app.

## Purpose and scope

The app was previously reachable by anyone who could reach its address on the LAN,
with no protection at all. Phase 5 adds exactly one gate: **one shared
username/password for the whole app** — not per-user accounts, not roles. The
existing "Role (prototype)" selector in the header
(`app/dependencies.py get_actor_role`) is a completely separate, unauthenticated,
cosmetic label used only to tag who-did-what in the audit log; it is unrelated to
this login and was left exactly as it was.

## Data model

### AuthSession (`auth_sessions`)
| Field | Type | Notes |
|---|---|---|
| id | int | primary key |
| token | string, unique, indexed | opaque, `secrets.token_urlsafe(32)` |
| created_at | datetime (UTC) | informational only |

No `expires_at`/TTL column, and no code path anywhere checks the age of a session.
A session is valid for exactly as long as its row exists — that's what makes
sessions server-side-revocable (unlike a stateless/JWT-style token with a built-in
expiry) and is the entire mechanism behind "sessions never expire on their own".
There is no separate `User` table: this app has exactly one shared
username/password, not per-user accounts, so a session row just proves "this
browser knows the one shared password", nothing more.

## Credentials

`APP_USERNAME` and `APP_PASSWORD_HASH` (a bcrypt hash, **never** the plaintext
password) originate as environment variables (`.env` locally; Render dashboard env
vars in production). `.env.example` ships obviously-placeholder values
(`changeme`); the real `.env` (gitignored, never committed) holds the real
generated username and bcrypt hash.

**Post-incident redesign:** the value actually checked at login time is *not* read
from the environment directly. It lives in the `app_settings` table (`AppSetting`,
Phase 4's generic key/value store) under `auth_username` / `auth_password_hash`.
`app/services/auth_service.py sync_credentials_from_env()` copies the environment
into those two rows — stripped of surrounding whitespace — and is called from
`app/seed_data.py seed_master_data()` on **every** app startup, not only when the
database is first created. `get_app_username(db)` / `get_app_password_hash(db)` /
`verify_credentials(db, ...)` all read from the DB rows, never from `os.getenv`
directly.

This replaced an earlier version that read `os.getenv("APP_PASSWORD_HASH")`
directly at check time on every call, with no stored copy at all. That was
correct in principle (no caching, no stale seed) but broke in practice: after
generating a new hash and updating `APP_PASSWORD_HASH` in Render's dashboard
followed by a redeploy, login kept failing. There was no seed-once bug to find —
credentials were never persisted anywhere — so the most likely real cause was a
stray leading/trailing newline or space introduced when pasting the hash into
Render's dashboard text field, which makes `bcrypt.checkpw` raise on a malformed
hash; `_verify_password` catches that and returns `False`, indistinguishable from
a genuinely wrong password. `sync_credentials_from_env()` strips both values
before storing them specifically to close that hole, and moving the check to a
DB row also means the credential is now something you can directly inspect/repair
via a DB query if it ever looks wrong again, instead of trusting an unlogged
`os.getenv` call.

Changing the credential does **not** touch `auth_sessions` — an already-logged-in
device stays logged in after a credential change, exactly as before. Only "Log out
everywhere" invalidates sessions.

## Session cookie

`eagle_session`, HttpOnly, `SameSite=Lax`, **not** `Secure` (the app runs over
plain HTTP on the LAN — the Secure flag would stop the browser from ever sending
the cookie back and lock everyone out), `max_age` ~10 years. The long max-age is
what keeps the cookie itself from expiring in the browser; the actual "never
expires" guarantee is that the server never checks the corresponding
`auth_sessions` row's age at all.

## API (`/api/v1/auth`)

- `POST /login` — body `{"username", "password"}`. Wrong username or password
  returns the standard `{"error": {"message", "field": "password"}}` envelope with
  status 400. On success, creates a new `AuthSession` row and sets the session
  cookie.
- `POST /logout` — deletes only the session row named by the current request's
  cookie (this device/browser). Every other active session (other devices) is
  untouched.
- `POST /logout-everywhere` — body `{"password"}`. Requires re-entering the
  current shared password (checked the same way `/login` checks it) to confirm
  this destructive, all-devices action, then deletes **every** `AuthSession` row
  at once — including the caller's own, which must then log in again like every
  other device.

## What's protected

`app/auth_middleware.py LoginRequiredMiddleware` enforces the login on **every**
route — UI pages and API endpoints — except a small allowlist:

- `GET /api/v1/health` (liveness check)
- `GET`/`POST /login` and `POST /api/v1/auth/login` (the login flow itself)
- static assets under `/static/` (CSS/JS needed to render the login page — no shop
  data lives there)

`/uploads/*` (actual defect photos) is deliberately **not** in the allowlist.
A missing/invalid session returns `401` with the standard error envelope for any
`/api/*` path, or a `303` redirect to `/login?next=<original path>` for a page.

## UI

- `GET /login` — a standalone page (not part of the normal nav), posts to
  `/api/v1/auth/login`, then redirects to `?next=` (default `/`) on success.
- `GET /settings` — behind the login like every other page. Has a plain "Log out"
  button (ends only this device) and a "Log out everywhere" form that requires
  re-entering the current password before it will submit.
- The header nav gained a "Settings" link; nothing else in the existing nav/role
  selector changed.

## Testing

`tests/api/test_auth_api.py` uses its own unauthenticated `raw_client` fixture
(with `monkeypatch.setenv` for known test credentials) to test the login flow
itself: every other test file's shared `client`/`mcp_env` fixtures pre-authenticate
by creating an `AuthSession` row directly and attaching its cookie, so ~everything
else in the suite can ignore auth entirely, the same way a browser stays logged in
across many requests once it has a valid cookie.

## MCP

The MCP server is just another client of the same REST API (`CLAUDE.md`
architecture rule), so it needs to log in too. `DEFECT_API_USERNAME` /
`DEFECT_API_PASSWORD` (plaintext — supplied to the MCP server's own process
environment, separate from the FastAPI app's own `.env` which never holds the
plaintext password) let it log in once on the first `401` and reuse the resulting
session cookie for every later call. See `docs/MCP_SETUP.md`.
