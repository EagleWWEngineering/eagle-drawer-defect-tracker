# Learning Guide

This project touches almost every layer of a modern web application. This guide
walks through one example end to end, explains the vocabulary using manufacturing
analogies, and maps out where to make common changes.

## One example, start to finish

**Scenario:** a QC person finds sanding scratches on one drawer for work order
WO-1024 and logs it on the New Defect screen.

1. **The operator fills out the form** on `/defect-entry` (rendered from
   `app/templates/defect_entry.html`) and clicks **Save defect**.
2. **JavaScript builds a JSON object** (`app/static/js/api.js`'s `Api.createDefectCase`)
   from the form fields — work order number, found station, one category row
   ("Sanding / Surface", quantity 1), priority, etc.
3. **The browser sends `POST /api/v1/defect-cases`** with that JSON as the request
   body, over plain HTTP to `127.0.0.1:8000`.
4. **FastAPI routes the request** to `create_case()` in
   `app/routers/defect_cases.py`. FastAPI has already parsed the JSON.
5. **Pydantic validates it** against `DefectCaseCreate` in `app/schemas.py` — is
   `work_order_number` a non-empty string? Is `items` at least one entry? If not, the
   request is rejected before any of our code even runs.
6. **The service layer applies the counting and business rules**:
   `create_defect_case()` in `app/services/defect_service.py` checks the priority is
   valid, the stations/categories exist, merges any duplicate categories in the
   submission, generates the case number (`DF-20260724-0001`), and decides the
   initial status (`Open`).
7. **SQLAlchemy saves the rows**: one `DefectCase` row and one `DefectItem` row
   (`app/models.py`), inside one database transaction.
8. **SQLite persists them** to `data/defect_tracker.db` on disk — the data survives
   even if the app is stopped and restarted.
9. **The router returns JSON** describing the saved case, including
   `"defect_event_count": 1` and the new case number, which the browser shows as a
   confirmation.
10. **Later, the Dashboard requests summary JSON**:
    `GET /api/v1/reports/summary?start_date=...&end_date=...` — this goes through the
    exact same router → service → database path, just reading instead of writing.
11. **An MCP tool requests the same summary** (e.g. `get_defect_summary` in
    `mcp_server/server.py`) by calling that *same* `GET /api/v1/reports/summary` URL
    with `httpx` — not by opening the SQLite file itself. That's why the numbers a
    connected AI tool reports can never disagree with what's on the Dashboard.
12. **Codex or Claude Code explains the result** in plain language to whoever asked,
    using the MCP tool's structured JSON response as its source of truth — and, per
    the server's instructions, is careful never to call the possible-source-station
    field a "confirmed root cause."

## Vocabulary, with manufacturing analogies

- **UI (user interface)** — the screens on the browser: forms, buttons, charts. Think
  of it as the paper traveler/routing sheet that rides along with a drawer — a
  human-friendly way to see and record what's happening.
- **Backend** — the program running on the server (`uvicorn app.main:app`) that
  actually does the work behind the UI. Like the shop's back office: nobody on the
  floor sees it directly, but it's what makes the paperwork mean something.
- **API (application programming interface)** — the fixed set of "requests" the
  backend understands, like `POST /api/v1/defect-cases`. Comparable to a standard
  shop traveler form: everyone fills it out the same way, so anyone reading it knows
  what each field means.
- **JSON** — the plain-text data format used to send information back and forth
  (`{"work_order_number": "WO-1024", ...}`). Think of it as the standardized fields on
  that traveler form, just typed instead of handwritten.
- **Database** — where data is permanently stored (SQLite, one file:
  `data/defect_tracker.db`). The filing cabinet that keeps every traveler ever
  completed, long after the drawer has shipped.
- **MCP host** — the application a person is chatting with (Claude Code, Codex). The
  "front desk" a person talks to.
- **MCP client** — the part of the host that actually opens a connection to one MCP
  server. The front desk's phone line to a specific department.
- **MCP server** — `mcp_server/server.py`, a small program that answers a fixed menu
  of requests ("tools") about this project's data, the same way the REST API does for
  the browser. The quality department's designated contact person, who only answers
  questions using the shop's real paperwork (the REST API), never by rummaging through
  the filing cabinet directly.
- **Tool** — one specific thing an MCP server can do, like `get_rework_queue` or
  `record_defect_case`. One specific request you could make to that contact person.
- **Resource** — a document an MCP server can hand over as-is, like this project's
  `quality://defect-tracker/data-dictionary` (the data dictionary file). A reference
  binder the contact person can hand you.
- **Prompt** — a canned, reusable question template, like this project's
  `weekly_quality_review`. A pre-written checklist for a recurring task.

## File map: where to make common changes

| I want to... | Look here |
|---|---|
| Add/rename a station or defect category | `app/seed_data.py` (initial seed) and the Admin screen at runtime |
| Change a counting formula or validation rule | `app/services/defect_service.py`, `app/services/metrics_service.py` |
| Change what a status can transition to | `STATUS_TRANSITIONS` in `app/services/defect_service.py` |
| Add a new REST endpoint | `app/routers/*.py`, wired up in `app/main.py` |
| Change what a JSON request/response looks like | `app/schemas.py` |
| Add a database column | `app/models.py`, then `alembic revision --autogenerate -m "..."` and `alembic upgrade head` |
| Change a screen's layout or add a field to a form | `app/templates/*.html` |
| Change shared page behavior (toasts, badges, role selector) | `app/static/js/app.js` |
| Change how the API is called from the browser | `app/static/js/api.js` |
| Change chart rendering | `app/static/js/charts.js` |
| Add or change an MCP tool | `mcp_server/server.py` |
| Generate demo data to explore the app | `scripts/seed_demo_data.py` |

## Why the UI and MCP server call the same API

This is the single most important architectural decision in the project: both paths
(`app/templates/*.html` via `app/static/js/api.js`, and `mcp_server/server.py` via
`httpx`) end up calling the exact same FastAPI routes, which call the exact same
service-layer functions. There is only one place the counting rules, status
transitions, and validation live — `app/services/`. If a rule ever needs to change,
it changes in one file, and both the browser and any connected AI assistant see the
new behavior automatically.
