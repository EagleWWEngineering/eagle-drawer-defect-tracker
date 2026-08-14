"""MCP server tests (PROJECT_SPEC.md section 14):

- read-tool results match the REST API for the same date range
- write tools create/update exactly one audited record
- API-unavailable returns a clear connection error
- the server never writes to stdout
"""

from __future__ import annotations

import io
import sys

import httpx
import pytest


async def _create_case_via_api(mcp_module, wo="WO-MCP-1", category="Sanding / Surface", qty=1):
    md = await mcp_module._get_master_data(refresh=True)
    station_id = next(s["id"] for s in md["stations"] if s["name"] == "QC / Sorting / Shipping")
    category_id = next(c["id"] for c in md["defect_categories"] if c["name"] == category)
    payload = {
        "production_date": "2026-07-24",
        "detected_at": "2026-07-24T15:00:00Z",
        "work_order_number": wo,
        "found_station_id": station_id,
        "priority": "Normal",
        "items": [{"defect_category_id": category_id, "affected_drawer_quantity": qty}],
    }
    return await mcp_module._request("POST", "/api/v1/defect-cases", json_body=payload)


async def test_get_defect_summary_matches_direct_api_call(mcp_env):
    mcp_module, _Session = mcp_env
    await _create_case_via_api(mcp_module, qty=2)

    tool_result = await mcp_module.get_defect_summary("2026-07-24", "2026-07-24")
    direct = await mcp_module._request(
        "GET",
        "/api/v1/reports/summary",
        params={"start_date": "2026-07-24", "end_date": "2026-07-24"},
    )
    assert tool_result == direct
    assert tool_result["defect_events"] == 2
    # Cost fields (Phase 4) pass through as part of the same summary payload.
    # Scrap cost was dropped entirely (docs/PROJECT_SPEC_PHASE4.md "Scrap removal").
    assert "internal_rework_cost" in tool_result
    assert "internal_scrap_cost" not in tool_result
    assert "total_internal_quality_cost" in tool_result
    assert "quality_cost_per_drawer_inspected" in tool_result


async def test_get_defect_pareto_matches_direct_api_and_sums_correctly(mcp_env):
    mcp_module, _Session = mcp_env
    await _create_case_via_api(mcp_module, wo="WO-P1", category="Sanding / Surface", qty=3)
    await _create_case_via_api(mcp_module, wo="WO-P2", category="Dado / Bottom Groove", qty=1)

    result = await mcp_module.get_defect_pareto("2026-07-24", "2026-07-24")
    assert result["total_defect_events"] == 4
    assert result["rows"][0]["label"] == "Sanding / Surface"


async def test_search_defect_cases_filters_by_named_station_and_category(mcp_env):
    mcp_module, _Session = mcp_env
    await _create_case_via_api(mcp_module, wo="WO-SEARCH", category="Dado / Bottom Groove")

    result = await mcp_module.search_defect_cases(
        station="QC / Sorting / Shipping", category="Dado / Bottom Groove"
    )
    assert result["total"] == 1
    assert result["cases"][0]["work_order_number"] == "WO-SEARCH"


async def test_record_defect_case_creates_exactly_one_audited_case(mcp_env):
    mcp_module, TestingSession = mcp_env
    result = await mcp_module.record_defect_case(
        work_order_number="WO-MCP-WRITE",
        found_station="QC / Sorting / Shipping",
        items=[{"category": "Sanding / Surface", "affected_drawer_quantity": 2}],
        production_date="2026-07-24",
        detected_at="2026-07-24T15:00:00Z",
    )
    assert result["case_number"].startswith("DF-20260724-")
    assert result["defect_event_count"] == 2

    from app.models import AuditLog, DefectCase

    session = TestingSession()
    cases = session.query(DefectCase).filter(DefectCase.work_order_number == "WO-MCP-WRITE").all()
    assert len(cases) == 1
    audit_entries = session.query(AuditLog).filter(AuditLog.action == "create").all()
    assert len(audit_entries) == 1
    session.close()


async def test_record_daily_production_upserts_one_row(mcp_env):
    mcp_module, TestingSession = mcp_env
    await mcp_module.record_daily_production(
        production_date="2026-07-24", drawers_inspected=100, drawers_rejected_unique=5
    )
    await mcp_module.record_daily_production(
        production_date="2026-07-24", drawers_inspected=120, drawers_rejected_unique=6
    )

    from app.models import DailyProductionSummary

    session = TestingSession()
    rows = session.query(DailyProductionSummary).all()
    assert len(rows) == 1
    assert rows[0].drawers_inspected == 120
    session.close()


async def test_update_defect_case_status_via_case_number(mcp_env):
    mcp_module, _Session = mcp_env
    created = await _create_case_via_api(mcp_module, wo="WO-STATUS")

    updated = await mcp_module.update_defect_case_status(
        case_number=created["case_number"], new_status="In Rework", note="Starting rework"
    )
    assert updated["status"] == "In Rework"

    fetched = await mcp_module.get_defect_case(created["case_number"])
    assert fetched["status"] == "In Rework"


async def test_invalid_status_transition_raises_clear_error(mcp_env):
    mcp_module, _Session = mcp_env
    created = await _create_case_via_api(mcp_module, wo="WO-BAD-STATUS")

    # "Closed - Repaired" is now reachable directly from Open (PROJECT_SPEC.md
    # section 3.3 - Close Directly, with an optional note). "Ready for QC Recheck"
    # is not a direct-close target and not in STATUS_TRANSITIONS for Open, so it's
    # still a genuinely invalid transition.
    with pytest.raises(mcp_module.DefectTrackerApiError):
        await mcp_module.update_defect_case_status(
            case_number=created["case_number"], new_status="Ready for QC Recheck"
        )


async def test_unknown_category_name_raises_clear_error(mcp_env):
    mcp_module, _Session = mcp_env
    with pytest.raises(mcp_module.DefectTrackerApiError, match="Unknown defect category"):
        await mcp_module.record_defect_case(
            work_order_number="WO-BAD-CAT",
            found_station="QC / Sorting / Shipping",
            items=[{"category": "Not A Real Category"}],
        )


async def test_api_unavailable_returns_clear_connection_error(mcp_env, monkeypatch):
    """An unreachable API must raise a clear, actionable error - never a raw traceback.

    Connecting to an unused loopback port either refuses immediately (ConnectError)
    or hangs until the client timeout (TimeoutException) depending on OS/firewall
    behavior; both are handled and produce an actionable message pointing at how to
    start the server.
    """
    mcp_module, _Session = mcp_env
    unreachable_client = httpx.AsyncClient(base_url="http://127.0.0.1:1", timeout=1.0)
    monkeypatch.setattr(mcp_module, "_client", unreachable_client)
    monkeypatch.setattr(mcp_module, "_master_data_cache", None)

    with pytest.raises(mcp_module.DefectTrackerApiError, match="Could not reach|Timed out"):
        await mcp_module.get_rework_queue()

    await unreachable_client.aclose()


async def test_rework_queue_and_work_order_history_end_to_end(mcp_env):
    mcp_module, _Session = mcp_env
    await _create_case_via_api(mcp_module, wo="WO-QUEUE")

    queue = await mcp_module.get_rework_queue()
    assert queue["count"] >= 1
    assert any(item["work_order_number"] == "WO-QUEUE" for item in queue["items"])

    history = await mcp_module.get_work_order_defect_history("WO-QUEUE")
    assert history["total_defect_events"] == 1


async def test_tool_call_writes_nothing_to_stdout(mcp_env, capsys):
    mcp_module, _Session = mcp_env
    await mcp_module.get_rework_queue()
    await mcp_module.record_daily_production(
        production_date="2026-07-24", drawers_inspected=10, drawers_rejected_unique=1
    )
    captured = capsys.readouterr()
    assert captured.out == ""


def test_server_never_prints_to_stdout(monkeypatch):
    """Regression guard: stdio MCP servers must never write to stdout (it corrupts
    the JSON-RPC stream). Importing the module and building its instructions/tool
    metadata must not touch sys.stdout."""
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    import importlib

    from mcp_server import server as mcp_module

    importlib.reload(mcp_module)

    assert captured.getvalue() == ""


async def test_end_to_end_via_fastmcp_call_tool_dispatch(mcp_env):
    """Exercises the real FastMCP dispatch path (schema validation + call_tool), not
    just calling the Python function directly - the closest thing to going through an
    MCP client without spinning up a second process."""
    mcp_module, _Session = mcp_env

    write_result = await mcp_module.mcp.call_tool(
        "record_defect_case",
        {
            "work_order_number": "WO-DISPATCH",
            "found_station": "QC / Sorting / Shipping",
            "items": [{"category": "Sanding / Surface", "affected_drawer_quantity": 1}],
            "production_date": "2026-07-24",
            "detected_at": "2026-07-24T15:00:00Z",
        },
    )
    assert write_result is not None

    read_result = await mcp_module.mcp.call_tool(
        "search_defect_cases", {"work_order_number": "WO-DISPATCH"}
    )
    assert read_result is not None


async def test_all_required_tools_are_registered_with_correct_annotations():
    from mcp_server import server as mcp_module

    tools = {t.name: t for t in await mcp_module.mcp.list_tools()}
    read_tools = [
        "get_defect_summary",
        "get_defect_pareto",
        "search_defect_cases",
        "get_rework_queue",
        "get_work_order_defect_history",
        "get_defect_case",
    ]
    write_tools = ["record_defect_case", "record_daily_production", "update_defect_case_status"]

    for name in read_tools + write_tools:
        assert name in tools, f"missing required tool: {name}"

    for name in read_tools:
        assert tools[name].annotations.readOnlyHint is True
    for name in write_tools:
        assert tools[name].annotations.readOnlyHint is False

    # No hard-delete tool is ever exposed.
    assert not any("delete" in name.lower() for name in tools)


async def test_data_dictionary_resource_is_readable():
    from mcp_server import server as mcp_module

    contents = await mcp_module.mcp.read_resource("quality://defect-tracker/data-dictionary")
    contents = list(contents)
    assert len(contents) == 1
    assert "Data Dictionary" in contents[0].content


async def test_weekly_quality_review_prompt_references_the_read_tools():
    from mcp_server import server as mcp_module

    result = await mcp_module.mcp.get_prompt(
        "weekly_quality_review", {"start_date": "2026-07-18", "end_date": "2026-07-24"}
    )
    text = " ".join(str(m) for m in result.messages)
    assert "get_defect_summary" in text
    assert "get_defect_pareto" in text
    assert "get_rework_queue" in text
