"""A tool the deployment has not licensed must not exist, not merely refuse.

A refused tool still appears in the agent's prompt and still gets called; a tool
that was never registered cannot be.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

READ_TOOLS = {
    "workday_find_worker",
    "workday_get_worker",
    "workday_list_organizations",
    "workday_list_reference_ids",
    "workday_list_job_profiles",
    "workday_list_locations",
    "workday_list_pre_hires",
    "workday_get_payment_elections",
    "workday_get_event",
    "workday_get_organization",
    "workday_get_manager_organizations",
    "workday_list_organization_workers",
    "workday_list_positions",
    "workday_get_worker_event_history",
    "workday_get_journal",
}

WRITE_TOOLS = {
    "workday_create_pre_hire",
    "workday_hire_employee",
    "workday_change_job",
    "workday_change_organization_assignments",
    "workday_terminate_employee",
    "workday_set_payment_elections",
    "workday_create_supervisory_organization",
    "workday_inactivate_supervisory_organization",
    "workday_update_supervisory_organization",
    "workday_assign_organization_role",
    "workday_create_position",
    "workday_update_home_address",
    "workday_recover_event",
    "workday_submit_accounting_journal",
}

# Read-only, but it exists to precede a write, so it ships with the writes.
PREVIEW_TOOL = "workday_preview_write"

DESTRUCTIVE_TOOLS = WRITE_TOOLS - {
    "workday_create_pre_hire",
    "workday_hire_employee",
    "workday_change_job",
    "workday_change_organization_assignments",
}


async def _tool_names(mcp: FastMCP) -> set[str]:
    return {tool.name for tool in await mcp.list_tools()}


async def test_full_server_registers_every_tool(server_factory) -> None:
    assert await _tool_names(server_factory()) == READ_TOOLS | WRITE_TOOLS | {
        PREVIEW_TOOL
    }


async def test_read_only_server_registers_only_read_tools(server_factory) -> None:
    assert await _tool_names(server_factory(read_only=True)) == READ_TOOLS


async def test_read_only_is_driven_by_the_environment(server_factory, set_env) -> None:
    set_env(WORKDAY_READ_ONLY="true")
    assert await _tool_names(server_factory()) == READ_TOOLS


async def test_read_only_server_cannot_be_asked_to_write(server_factory) -> None:
    mcp = server_factory(read_only=True)
    # Must fail because the tool is absent, not merely because the call errored.
    with pytest.raises(ToolError, match="Unknown tool"):
        await mcp.call_tool("workday_terminate_employee", {})


async def test_enabled_tools_narrows_the_surface_further(
    server_factory, set_env
) -> None:
    set_env(WORKDAY_ENABLED_TOOLS="workday_get_worker,workday_change_job")
    assert await _tool_names(server_factory()) == {
        "workday_get_worker",
        "workday_change_job",
    }


async def test_payment_tools_can_be_withheld_entirely(server_factory, set_env) -> None:
    """A customer who does not want bank details touched can exclude them."""
    keep = sorted(
        (READ_TOOLS | WRITE_TOOLS)
        - {
            "workday_get_payment_elections",
            "workday_set_payment_elections",
        }
    )
    set_env(WORKDAY_ENABLED_TOOLS=",".join(keep))
    names = await _tool_names(server_factory())
    assert "workday_set_payment_elections" not in names
    assert "workday_get_payment_elections" not in names
    assert "workday_change_job" in names


async def test_read_tools_are_annotated_read_only(server_factory) -> None:
    for tool in await server_factory().list_tools():
        if tool.name in READ_TOOLS | {PREVIEW_TOOL}:
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.destructiveHint is False


async def test_only_terminate_and_payments_are_annotated_destructive(
    server_factory,
) -> None:
    for tool in await server_factory().list_tools():
        if tool.name in WRITE_TOOLS:
            assert tool.annotations.readOnlyHint is False
            assert tool.annotations.destructiveHint is (tool.name in DESTRUCTIVE_TOOLS)


async def test_every_tool_has_a_title_and_a_real_description(server_factory) -> None:
    for tool in await server_factory().list_tools():
        assert tool.annotations.title
        assert tool.description and len(tool.description) > 40


async def test_write_descriptions_point_at_the_preview(server_factory) -> None:
    """The agent should be told how to show a person what a write would do."""
    tools = {tool.name: tool for tool in await server_factory().list_tools()}
    assert "workday_preview_write" in tools[PREVIEW_TOOL].name
    assert (
        "workday_change_job"
        in tools["workday_change_organization_assignments"].description
    )
