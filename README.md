# Eagle Drawer Defect Tracker

A local-first defect tracking system for Eagle Woodworking's drawer-production pilot:
a FastAPI + SQLite web app for QC and the Manufacturing Engineer, plus a stdio MCP
server so Claude Code / Codex can query and (with confirmation) record data using the
same rules as the UI.

Full specification: [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md)
(Phase 2 Customer Issues addendum: [`docs/PROJECT_SPEC_PHASE2.md`](docs/PROJECT_SPEC_PHASE2.md);
Phase 3 production brief sync addendum: [`docs/PROJECT_SPEC_PHASE3.md`](docs/PROJECT_SPEC_PHASE3.md);
Phase 4 internal cost tracking addendum: [`docs/PROJECT_SPEC_PHASE4.md`](docs/PROJECT_SPEC_PHASE4.md);
Phase 5 single shared login addendum: [`docs/PROJECT_SPEC_PHASE5.md`](docs/PROJECT_SPEC_PHASE5.md)).
Field reference: [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md).
How to use the app day to day: [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md).
New to web apps/APIs/databases/MCP? Start with [`docs/LEARNING_GUIDE.md`](docs/LEARNING_GUIDE.md).
Connecting an MCP client: [`docs/MCP_SETUP.md`](docs/MCP_SETUP.md).

## Quick start (Windows)

```
run_app.bat
```

## Quick start (macOS/Linux)

```
./run_app.sh
```

## Manual setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
copy .env.example .env      # macOS/Linux: cp .env.example .env
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Then open http://127.0.0.1:8000 in a browser and log in with the shared
username/password (see below) — every page requires it.

### Setting the real shared login before first use

`.env.example`'s `APP_USERNAME`/`APP_PASSWORD_HASH` are placeholders, not a working
login — replace them in your real `.env` (never commit it) before starting the app
for real use:

```bash
python -c "import bcrypt; print(bcrypt.hashpw(b'your-real-password', bcrypt.gensalt()).decode())"
```

Set `APP_USERNAME=<your chosen username>` and `APP_PASSWORD_HASH=<the printed hash>`
in `.env`. See [`docs/PROJECT_SPEC_PHASE5.md`](docs/PROJECT_SPEC_PHASE5.md).

Optional synthetic demo data:

```bash
python scripts/seed_demo_data.py
python scripts/seed_customer_issues.py
```

## Tests and linting

```bash
pytest
ruff check .
```

## MCP server

```bash
python -m mcp_server.server
```

See [`docs/MCP_SETUP.md`](docs/MCP_SETUP.md) for Codex and Claude Code configuration.

## Project status

This is an MVP pilot for drawer production only (no cabinet doors, no employee
performance tracking). It runs behind a single shared login (see
`docs/PROJECT_SPEC_PHASE5.md`) — see `docs/PROJECT_SPEC.md` section 8 for the
full safety/reliability notes before any further deployment.
