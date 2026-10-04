"""End-to-end tests over the real stdio transport.

These spawn the server as a subprocess and speak MCP to it, covering the path it
is actually deployed on. The unit tests exercise tool logic in process, which
cannot catch a server that fails to start, an entry point that does not resolve,
or anything written to stdout corrupting the JSON-RPC stream.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import LATEST_PROTOCOL_VERSION, TextContent

SRC = str(Path(__file__).resolve().parent.parent / "src")
STARTUP_TIMEOUT_SECONDS = 60

TOTAL_TOOLS = 31
READ_ONLY_TOOLS = 16

# An unroutable address, so a tool call exercises the failure path without
# reaching Workday. Port 9 (discard) refuses or drops connections immediately.
UNROUTABLE_HOST = "127.0.0.1:9"


def _server_env(**overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    # Let the subprocess import the package whether or not it is installed.
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    for name in (
        "WORKDAY_READ_ONLY",
        "WORKDAY_ENABLED_TOOLS",
        "WORKDAY_ALLOW_AUTO_COMPLETE",
    ):
        env.pop(name, None)
    env.setdefault("WORKDAY_HOST", "wd3-impl-services1.workday.com")
    env.setdefault("WORKDAY_TENANT", "acme_preview")
    env.setdefault("WORKDAY_ISU_USERNAME", "isu_harriet")
    env.setdefault("WORKDAY_ISU_PASSWORD", "isu-password")
    env.update(overrides)
    return env


def _params(**env_overrides: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "workday_advanced_mcp"],
        env=_server_env(**env_overrides),
    )


async def _with_session(params: StdioServerParameters, body):
    async def run():
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            init = await session.initialize()
            return await body(session, init)

    return await asyncio.wait_for(run(), timeout=STARTUP_TIMEOUT_SECONDS)


def _text_of(result) -> str:
    """Pull the text out of a tool result, which is a union of content types."""
    return "".join(
        block.text for block in result.content if isinstance(block, TextContent)
    )


async def test_server_starts_and_completes_handshake() -> None:
    async def body(session: ClientSession, init):
        return init.serverInfo.name

    assert await _with_session(_params(), body) == "workday_advanced_mcp"


async def test_server_lists_every_tool_over_stdio() -> None:
    async def body(session: ClientSession, _init):
        return {tool.name for tool in (await session.list_tools()).tools}

    names = await _with_session(_params(), body)
    assert len(names) == TOTAL_TOOLS
    assert "workday_get_worker" in names
    assert "workday_change_organization_assignments" in names


async def test_read_only_mode_hides_write_tools_over_stdio() -> None:
    async def body(session: ClientSession, _init):
        return {tool.name for tool in (await session.list_tools()).tools}

    names = await _with_session(_params(WORKDAY_READ_ONLY="true"), body)
    assert len(names) == READ_ONLY_TOOLS
    assert not any(
        name.startswith(
            ("workday_change", "workday_terminate", "workday_hire", "workday_create")
        )
        for name in names
    )
    assert "workday_set_payment_elections" not in names


async def test_the_server_starts_without_credentials_and_says_what_is_missing() -> None:
    """A misconfigured server must still serve, and name what to set."""

    async def body(session: ClientSession, _init):
        result = await session.call_tool("workday_get_worker", {"worker_id": "21001"})
        return _text_of(result)

    text = await _with_session(
        _params(WORKDAY_ISU_USERNAME="", WORKDAY_ISU_PASSWORD=""), body
    )
    assert text.startswith("Error:")
    assert "WORKDAY_ISU_PASSWORD" in text


async def test_a_validation_error_is_returned_without_touching_the_network() -> None:
    """Pre-flight validation surfaces as a normal tool result, not a crash."""

    async def body(session: ClientSession, _init):
        result = await session.call_tool(
            "workday_change_job",
            {
                "changes": [{"worker_id": "21001", "business_title": "x"}],
                "effective_date": "2026-04-01",
                "reason_id": "",
            },
        )
        return _text_of(result)

    text = await _with_session(_params(WORKDAY_HOST=UNROUTABLE_HOST), body)
    assert text.startswith("Error:")
    assert "workday_list_reference_ids" in text


async def test_an_unreachable_tenant_is_reported_not_crashed() -> None:
    async def body(session: ClientSession, _init):
        tools = await session.list_tools()
        result = await session.call_tool("workday_get_worker", {"worker_id": "21001"})
        return len(tools.tools), _text_of(result)

    tool_count, text = await _with_session(
        _params(WORKDAY_HOST=UNROUTABLE_HOST, WORKDAY_HTTP_TIMEOUT="2"), body
    )
    assert tool_count == TOTAL_TOOLS
    assert text.startswith("Error:")


async def test_stdout_carries_only_json_rpc() -> None:
    """Every byte on stdout must be JSON-RPC; diagnostics belong on stderr.

    The SDK client tolerates junk lines, so this drives the protocol by hand and
    inspects the raw stream. Without it, a stray print would go unnoticed here
    and corrupt the transport for stricter clients.

    The environment makes the server as talkative as possible: it warns about the
    non-Workday host and logs through the MCP library.
    """
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "workday_advanced_mcp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_server_env(WORKDAY_HOST=UNROUTABLE_HOST),
    )

    stdin, stdout = process.stdin, process.stdout
    assert stdin is not None
    assert stdout is not None

    async def send(message: dict) -> None:
        stdin.write((json.dumps(message) + "\n").encode())
        await stdin.drain()

    try:
        await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

        lines: list[bytes] = []
        while True:
            line = await asyncio.wait_for(
                stdout.readline(), timeout=STARTUP_TIMEOUT_SECONDS
            )
            assert line, "server closed stdout before answering tools/list"
            lines.append(line)
            if b'"id":2' in line.replace(b" ", b""):
                break
    finally:
        stdin.close()
        process.terminate()
        stderr = (await process.communicate())[1]

    for line in lines:
        text = line.decode().strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:  # pragma: no cover - only on regression
            pytest.fail(f"non-JSON data written to stdout: {text!r}")
        assert message.get("jsonrpc") == "2.0", f"not a JSON-RPC message: {text!r}"

    # The diagnostics really did happen, just on the correct stream.
    assert b"not a *.workday.com host" in stderr


def test_console_script_entry_point_is_declared() -> None:
    """The sandbox runs `uvx ... workday-advanced-mcp`, so this must resolve."""
    from importlib.metadata import distribution

    try:
        entry_points = distribution("workday-advanced-mcp").entry_points
    except Exception:  # pragma: no cover - only when running from a bare checkout
        pytest.skip("package is not installed")

    console = {
        ep.name: ep.value for ep in entry_points if ep.group == "console_scripts"
    }
    assert console.get("workday-advanced-mcp") == "workday_advanced_mcp:main"
