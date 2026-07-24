# MCP Setup

The Eagle Drawer Defect Tracker MCP server is a separate **stdio** process
(`mcp_server/server.py`) that calls the same REST API the browser UI uses. It never
touches SQLite directly.

**Before connecting any MCP client, start the FastAPI app first** — the MCP server
has nothing to talk to otherwise:

```bash
# from the project root, with the virtual environment active
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The MCP server reads `DEFECT_API_URL` from its environment (default
`http://127.0.0.1:8000`), so it only needs to know where the FastAPI app is running —
it does not need its own copy of `.env` or database credentials.

Replace `<PROJECT_ROOT>` below with the absolute path to this project on your machine,
and `<PYTHON>` with the absolute path to the project's virtual environment Python
(see the platform-specific commands below to find it).

## Codex CLI

### Option A: `codex mcp add` (quickest)

**macOS/Linux:**
```bash
codex mcp add eagle-drawer-defect-tracker \
  --env DEFECT_API_URL=http://127.0.0.1:8000 \
  -- <PROJECT_ROOT>/.venv/bin/python -m mcp_server.server
```

**Windows (PowerShell):**
```powershell
codex mcp add eagle-drawer-defect-tracker `
  --env DEFECT_API_URL=http://127.0.0.1:8000 `
  -- <PROJECT_ROOT>\.venv\Scripts\python.exe -m mcp_server.server
```

### Option B: project-scoped `.codex/config.toml`

Create `<PROJECT_ROOT>/.codex/config.toml`:

```toml
[mcp_servers.eagle-drawer-defect-tracker]
command = "<PYTHON>"                 # e.g. <PROJECT_ROOT>/.venv/bin/python
args = ["-m", "mcp_server.server"]
env = { DEFECT_API_URL = "http://127.0.0.1:8000" }
```

### List connected servers / run a test tool (Codex)

```bash
codex mcp list
codex mcp run eagle-drawer-defect-tracker get_defect_summary \
  '{"start_date": "2026-07-01", "end_date": "2026-07-24"}'
```

(Exact subcommands may vary by Codex CLI version — run `codex mcp --help` if these
differ.)

## Claude Code

### Option A: `claude mcp add` (quickest)

**macOS/Linux:**
```bash
claude mcp add eagle-drawer-defect-tracker \
  --env DEFECT_API_URL=http://127.0.0.1:8000 \
  -- <PROJECT_ROOT>/.venv/bin/python -m mcp_server.server
```

**Windows (PowerShell):**
```powershell
claude mcp add eagle-drawer-defect-tracker `
  --env DEFECT_API_URL=http://127.0.0.1:8000 `
  -- <PROJECT_ROOT>\.venv\Scripts\python.exe -m mcp_server.server
```

### Option B: project-scoped `.mcp.json`

Create `<PROJECT_ROOT>/.mcp.json` (do not commit real machine-specific paths if you
share this repo — treat this file as a local example, not a checked-in default):

```json
{
  "mcpServers": {
    "eagle-drawer-defect-tracker": {
      "command": "<PYTHON>",
      "args": ["-m", "mcp_server.server"],
      "env": {
        "DEFECT_API_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

### List connected servers / run a test tool (Claude Code)

```bash
claude mcp list
```

Then, inside a Claude Code session, ask it to call a read-only tool, e.g.:
"Use the eagle-drawer-defect-tracker MCP server's get_rework_queue tool."

## Finding `<PYTHON>` for the commands above

**Windows (PowerShell), from the project root:**
```powershell
(Resolve-Path ".\.venv\Scripts\python.exe").Path
```

**macOS/Linux, from the project root:**
```bash
realpath ./.venv/bin/python
```

## Verifying the connection without Codex or Claude Code (MCP Inspector)

The official MCP Inspector is a Node-based tool that talks to any stdio MCP server:

```bash
npx @modelcontextprotocol/inspector <PYTHON> -m mcp_server.server
```

It opens a local web UI where you can list tools/resources/prompts and call
`get_defect_summary` (read) or `record_daily_production` (write) directly to confirm
the server responds correctly. Set the `DEFECT_API_URL` environment variable in the
Inspector's launch config if the FastAPI app isn't at the default address.

If Node/npm isn't available, you can run the same check with the official Python SDK
client directly (no Node required) — see `scripts/` for a minimal example pattern
using `mcp.client.stdio.stdio_client` + `mcp.ClientSession`, the same approach used to
verify this server during development.

## What the server exposes

- **Read tools** (safe, no writes): `get_defect_summary`, `get_defect_pareto`,
  `search_defect_cases`, `get_rework_queue`, `get_work_order_defect_history`,
  `get_defect_case`.
- **Write tools** (create auditable records; only called when you explicitly ask):
  `record_defect_case`, `record_daily_production`, `update_defect_case_status`.
- **Resource**: `quality://defect-tracker/data-dictionary` (this project's
  `docs/DATA_DICTIONARY.md`).
- **Prompt**: `weekly_quality_review(start_date, end_date)`.

No tool can hard-delete a record. `possible_source_station` is always a hypothesis,
never a confirmed root cause — the server's instructions tell any connected model to
describe it that way.

## Troubleshooting

- **"Could not reach the Eagle Drawer Defect Tracker API..."** — start the FastAPI
  app first (`uvicorn app.main:app --host 127.0.0.1 --port 8000`), or check
  `DEFECT_API_URL` matches where it's actually running.
- **Tool calls hang or the client can't parse responses** — something is writing to
  stdout inside the server process (this should never happen in the shipped code;
  all logging goes to stderr). Check `mcp_server/server.py` hasn't been modified to
  add a stray `print()`.
- **"Unknown station"/"Unknown defect category"** — station and category names must
  match Admin screen entries exactly (case-insensitive); the error message lists the
  valid names.
