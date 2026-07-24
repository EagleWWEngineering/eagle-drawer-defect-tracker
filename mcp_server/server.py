"""Eagle Drawer Defect Tracker MCP server (stdio transport).

Exposes the same defect-tracking data as the web UI, by calling the FastAPI REST API
(never touches SQLite directly - see AGENTS.md / CLAUDE.md architecture rule, and
PROJECT_SPEC.md section 6). This keeps exactly one implementation of every counting
and validation rule: the service layer behind the REST API.

CRITICAL: stdio MCP servers must never print to stdout - that would corrupt the
JSON-RPC protocol stream. All logging here goes to stderr only (PROJECT_SPEC.md
section 6 / 8).

Run with: python -m mcp_server.server
Reads DEFECT_API_URL from the environment (default http://127.0.0.1:8000).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s eagle_defect_tracker_mcp: %(message)s",
)
logger = logging.getLogger("eagle_defect_tracker_mcp")

DEFECT_API_URL = os.environ.get("DEFECT_API_URL", "http://127.0.0.1:8000").rstrip("/")

SERVER_INSTRUCTIONS = """
Eagle Drawer Defect Tracker - quality data for Eagle Woodworking's drawer-production
pilot (drawers only; this is a process-improvement tool, not an employee-performance
tool - it does not track operator names).

READ TOOLS (get_defect_summary, get_defect_pareto, search_defect_cases,
get_rework_queue, get_work_order_defect_history, get_defect_case) are safe for
analysis and never change data.

IMPORTANT: "possible_source_station" (and Pareto grouped by "source_station") is a
HYPOTHESIS about where a defect may have originated, not a confirmed root cause.
Always describe it as "a possible/suspected source station", never as "the cause" or
"the confirmed root cause". The separate, optional "root_cause" field is the actual
investigated root cause, and it is frequently empty because that investigation happens
after the initial defect entry.

WRITE TOOLS (record_defect_case, record_daily_production, update_defect_case_status)
create or change auditable production records in the same database the shop floor
uses, and validate inputs the same way the web UI does (e.g. status transitions follow
a fixed map; reopening a closed case requires a note). Only call a write tool when the
user has explicitly asked you to record or change something - never proactively as a
side effect of answering a question. No tool here can hard-delete a record.
""".strip()

mcp = FastMCP(name="eagle-drawer-defect-tracker", instructions=SERVER_INSTRUCTIONS)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=DEFECT_API_URL, timeout=10.0)
    return _client


class DefectTrackerApiError(RuntimeError):
    """Raised for any REST API failure; FastMCP surfaces the message to the MCP client."""


def _extract_error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        message = data["error"].get("message", "Unknown error")
        field = data["error"].get("field")
        return f"{message} (field: {field})" if field else message
    if isinstance(data, dict) and "detail" in data:
        return str(data["detail"])
    return str(data)


async def _request(
    method: str, path: str, *, params: dict | None = None, json_body: dict | None = None
) -> Any:
    client = _get_client()
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        response = await client.request(method, path, params=clean_params, json=json_body)
    except httpx.ConnectError as exc:
        raise DefectTrackerApiError(
            f"Could not reach the Eagle Drawer Defect Tracker API at {DEFECT_API_URL}. "
            "Is the FastAPI server running? Start it with: "
            "uvicorn app.main:app --host 127.0.0.1 --port 8000"
        ) from exc
    except httpx.TimeoutException as exc:
        raise DefectTrackerApiError(
            f"Timed out waiting for the Eagle Drawer Defect Tracker API at {DEFECT_API_URL}."
        ) from exc

    if response.status_code >= 400:
        raise DefectTrackerApiError(_extract_error_message(response))
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


_master_data_cache: dict | None = None


async def _get_master_data(refresh: bool = False) -> dict[str, Any]:
    global _master_data_cache
    if _master_data_cache is None or refresh:
        _master_data_cache = await _request("GET", "/api/v1/master-data")
    return _master_data_cache


async def _station_id_by_name(name: str) -> int:
    md = await _get_master_data()
    for station in md["stations"]:
        if station["name"].lower() == name.lower():
            return station["id"]
    md = await _get_master_data(refresh=True)  # master data may have just changed in Admin
    for station in md["stations"]:
        if station["name"].lower() == name.lower():
            return station["id"]
    valid = ", ".join(s["name"] for s in md["stations"])
    raise DefectTrackerApiError(f"Unknown station '{name}'. Valid stations: {valid}")


async def _category_id_by_name(name: str) -> int:
    md = await _get_master_data()
    for category in md["defect_categories"]:
        if category["name"].lower() == name.lower():
            return category["id"]
    md = await _get_master_data(refresh=True)
    for category in md["defect_categories"]:
        if category["name"].lower() == name.lower():
            return category["id"]
    valid = ", ".join(c["name"] for c in md["defect_categories"])
    raise DefectTrackerApiError(f"Unknown defect category '{name}'. Valid categories: {valid}")


async def _resolve_named_filters(filters: dict | None) -> dict[str, Any]:
    """Translate a human-readable filters dict into the REST API's *_id query params.

    Accepted keys: work_order_number, category, found_station, possible_source_station,
    priority, status, disposition.
    """
    params: dict[str, Any] = {}
    if not filters:
        return params
    for key in ("work_order_number", "priority", "status", "disposition"):
        if filters.get(key):
            params[key] = filters[key]
    if filters.get("category"):
        params["category_id"] = await _category_id_by_name(filters["category"])
    if filters.get("found_station"):
        params["found_station_id"] = await _station_id_by_name(filters["found_station"])
    if filters.get("possible_source_station"):
        params["possible_source_station_id"] = await _station_id_by_name(
            filters["possible_source_station"]
        )
    return params


READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
ADDITIVE_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
UPSERT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
STATUS_CHANGE_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def get_defect_summary(
    start_date: str, end_date: str, filters: dict | None = None
) -> dict[str, Any]:
    """Get KPI totals for a date range: drawers inspected, defect events, unique
    drawers rejected, defects per 100 drawers, rejection rate, first pass yield,
    rework rate, and scrap rate. Rates are null/None when drawers_inspected is 0.

    Dates are "YYYY-MM-DD". filters is optional and may include any of:
    work_order_number, category (defect category name), found_station (station name),
    possible_source_station (station name - a HYPOTHESIS, not a confirmed cause),
    priority, status, disposition.
    """
    params = await _resolve_named_filters(filters)
    params["start_date"] = start_date
    params["end_date"] = end_date
    return await _request("GET", "/api/v1/reports/summary", params=params)


@mcp.tool(annotations=READ_ONLY)
async def get_defect_pareto(
    start_date: str,
    end_date: str,
    group_by: str = "category",
    limit: int = 10,
    filters: dict | None = None,
) -> dict[str, Any]:
    """Pareto of defect events, sorted highest to lowest with cumulative percentage.

    group_by: "category" (default, defect category name) or "source_station" (possible
    source station name - a HYPOTHESIS about where the defect may have originated, NOT
    a confirmed root cause; phrase any summary of this grouping accordingly).
    filters accepts the same keys as get_defect_summary.
    """
    params = await _resolve_named_filters(filters)
    params.update(
        {"start_date": start_date, "end_date": end_date, "group_by": group_by, "limit": limit}
    )
    rows = await _request("GET", "/api/v1/reports/pareto", params=params)
    return {"rows": rows, "total_defect_events": sum(r["defect_events"] for r in rows)}


@mcp.tool(annotations=READ_ONLY)
async def search_defect_cases(
    start_date: str | None = None,
    end_date: str | None = None,
    work_order_number: str | None = None,
    category: str | None = None,
    station: str | None = None,
    priority: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Search defect cases, newest first (up to 100 results).

    "station" filters by the FOUND station (where the defect was discovered) - to
    filter by possible source station instead, use get_defect_summary/get_defect_pareto
    with a filters dict containing "possible_source_station".
    """
    params: dict[str, Any] = {
        "start_date": start_date,
        "end_date": end_date,
        "work_order_number": work_order_number,
        "priority": priority,
        "status": status,
        "page_size": 100,
    }
    if category:
        params["category_id"] = await _category_id_by_name(category)
    if station:
        params["found_station_id"] = await _station_id_by_name(station)
    return await _request("GET", "/api/v1/defect-cases", params=params)


@mcp.tool(annotations=READ_ONLY)
async def get_rework_queue(
    priority: str | None = None, status: str | None = None
) -> dict[str, Any]:
    """Open rework-queue items, sorted Urgent first, then High, then Normal, oldest
    open item first within each priority. Excludes closed cases unless a closed
    `status` is explicitly requested."""
    rows = await _request(
        "GET", "/api/v1/rework-queue", params={"priority": priority, "status": status}
    )
    return {"items": rows, "count": len(rows)}


@mcp.tool(annotations=READ_ONLY)
async def get_work_order_defect_history(work_order_number: str) -> dict[str, Any]:
    """All defect cases recorded against one work order number, oldest first, with
    the total defect event count across all of them."""
    return await _request("GET", f"/api/v1/reports/work-orders/{work_order_number}")


@mcp.tool(annotations=READ_ONLY)
async def get_defect_case(case_number: str) -> dict[str, Any]:
    """Fetch one defect case by its case number, e.g. "DF-20260724-0001"."""
    return await _request("GET", f"/api/v1/defect-cases/by-number/{case_number}")


# ---------------------------------------------------------------------------
# Write tools - only call these when the user has explicitly asked for a write.
# ---------------------------------------------------------------------------


@mcp.tool(annotations=ADDITIVE_WRITE)
async def record_defect_case(
    work_order_number: str,
    found_station: str,
    items: list[dict],
    production_date: str | None = None,
    detected_at: str | None = None,
    drawer_part_reference: str | None = None,
    possible_source_station: str | None = None,
    priority: str = "Normal",
    disposition: str | None = None,
    repair_action: str | None = None,
    root_cause: str | None = None,
    corrective_action: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Create a new defect case (an auditable production record). Only call this when
    the user has explicitly asked you to record/log a defect.

    items: list of {"category": "<defect category name>", "affected_drawer_quantity": <int>}.
    "affected_drawer_quantity" defaults to 1 if omitted. One drawer with several
    physical defects in the SAME category is still one item with a higher quantity -
    do not create two items for the same category on one case.

    found_station: the station where the defect was actually found/discovered (required).
    possible_source_station: OPTIONAL hypothesis about where the defect may have
    originated. Never describe this to the user as a confirmed root cause.
    production_date/detected_at default to the current date/time if omitted
    ("YYYY-MM-DD" / ISO datetime).
    priority: "Urgent", "High", or "Normal" (default).
    """
    now = dt.datetime.now(dt.timezone.utc)
    resolved_items = []
    for item in items:
        category_id = await _category_id_by_name(item["category"])
        resolved_items.append(
            {
                "defect_category_id": category_id,
                "affected_drawer_quantity": item.get("affected_drawer_quantity", 1),
            }
        )

    payload = {
        "production_date": production_date or now.date().isoformat(),
        "detected_at": detected_at or now.isoformat(),
        "work_order_number": work_order_number,
        "drawer_part_reference": drawer_part_reference,
        "found_station_id": await _station_id_by_name(found_station),
        "possible_source_station_id": (
            await _station_id_by_name(possible_source_station) if possible_source_station else None
        ),
        "priority": priority,
        "items": resolved_items,
        "disposition": disposition,
        "repair_action": repair_action,
        "root_cause": root_cause,
        "corrective_action": corrective_action,
        "notes": notes,
    }
    return await _request("POST", "/api/v1/defect-cases", json_body=payload)


@mcp.tool(annotations=UPSERT_WRITE)
async def record_daily_production(
    production_date: str,
    drawers_inspected: int,
    drawers_rejected_unique: int,
    drawers_reworked: int = 0,
    drawers_scrapped: int = 0,
    shift: str = "Day",
    notes: str | None = None,
) -> dict[str, Any]:
    """Record (or update, if one already exists for this date+shift) the Daily
    Production Summary - the denominator for every rate on the dashboard. Only call
    this when the user has explicitly asked you to record production counts.

    Unusual combinations (e.g. drawers_reworked exceeding drawers_rejected_unique -
    which can legitimately happen when rework spans multiple days) are accepted only
    if `notes` explains why; otherwise the API rejects the entry and asks for a note.
    """
    payload = {
        "shift": shift,
        "drawers_inspected": drawers_inspected,
        "drawers_rejected_unique": drawers_rejected_unique,
        "drawers_reworked": drawers_reworked,
        "drawers_scrapped": drawers_scrapped,
        "notes": notes,
    }
    return await _request("PUT", f"/api/v1/daily-production/{production_date}", json_body=payload)


@mcp.tool(annotations=STATUS_CHANGE_WRITE)
async def update_defect_case_status(
    case_number: str,
    new_status: str,
    disposition: str | None = None,
    repair_action: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Change a defect case's status. Only call this when the user has explicitly
    asked you to update, advance, or close a case.

    Allowed statuses: "Open", "In Rework", "Waiting", "Ready for QC Recheck",
    "Closed - Repaired", "Closed - Scrapped", "Closed - Use As Is". Only specific
    transitions are allowed (enforced by the API, same rule as the web UI) - an
    invalid transition returns a clear error instead of silently failing. Reopening a
    closed case back to "Open" requires `note` to explain why.
    """
    case = await _request("GET", f"/api/v1/defect-cases/by-number/{case_number}")
    payload = {
        "new_status": new_status,
        "disposition": disposition,
        "repair_action": repair_action,
        "note": note,
    }
    return await _request("POST", f"/api/v1/defect-cases/{case['id']}/status", json_body=payload)


# ---------------------------------------------------------------------------
# Resource + prompt
# ---------------------------------------------------------------------------


@mcp.resource(
    "quality://defect-tracker/data-dictionary",
    name="Data Dictionary",
    description="Field definitions, counting rules, and API/MCP contracts for the tracker.",
    mime_type="text/markdown",
)
def data_dictionary_resource() -> str:
    path = Path(__file__).resolve().parent.parent / "docs" / "DATA_DICTIONARY.md"
    return path.read_text(encoding="utf-8")


@mcp.prompt()
def weekly_quality_review(start_date: str, end_date: str) -> str:
    """Draft a weekly quality review for the Manufacturing Engineer using the read tools."""
    return (
        f"Using the Eagle Drawer Defect Tracker MCP tools, prepare a weekly quality "
        f"review for {start_date} through {end_date}:\n"
        "1. Call get_defect_summary for the date range and report drawers inspected, "
        "defect events, rejection rate, first pass yield, rework rate, and scrap rate.\n"
        "2. Call get_defect_pareto grouped by category to find the top 3-5 defect "
        "categories driving events.\n"
        "3. Call get_defect_pareto grouped by source_station and summarize the top "
        "suspected source stations - describe these as hypotheses, never as a "
        "confirmed root cause.\n"
        "4. Call get_rework_queue to flag any Urgent or High priority items that have "
        "been open the longest.\n"
        "5. Summarize findings in plain language for the Manufacturing Engineer, and "
        "suggest 1-2 root-cause investigation priorities based on the Pareto results.\n"
        "Do not call any write tool during this review unless the user explicitly "
        "asks you to."
    )


def main() -> None:
    logger.info("Starting Eagle Drawer Defect Tracker MCP server (stdio). API: %s", DEFECT_API_URL)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
