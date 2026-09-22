"""MCP server exposing Workday staffing and payment-election writes.

Runs over stdio. Configuration comes from the environment; see config.py.
Nothing here touches the network at import or startup, so the server answers
``initialize`` immediately even when the tenant is slow or unreachable.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from mcp.server.fastmcp import FastMCP

from .config import (
    ENV_ALLOW_AUTO_COMPLETE,
    ENV_ENABLED_TOOLS,
    ENV_HOST,
    ENV_READ_ONLY,
    ENV_TENANT,
    load_settings,
)
from .registry import ToolRegistrar
from .tools_read import register_read_tools
from .tools_write import register_write_tools

SERVER_NAME = "workday_advanced_mcp"

# stdout carries the MCP protocol, so all logging goes to stderr.
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(SERVER_NAME)


def build_server(
    read_only: bool | None = None,
    enabled_tools: frozenset[str] | None = None,
) -> FastMCP:
    """Create the MCP server with the tools this deployment should expose."""
    settings = load_settings()
    if read_only is None:
        read_only = settings.read_only
    if enabled_tools is None:
        enabled_tools = settings.enabled_tools

    mcp = FastMCP(SERVER_NAME)
    registrar = ToolRegistrar(mcp, enabled_tools)
    register_read_tools(registrar)
    if not read_only:
        register_write_tools(registrar)
    return mcp


def _print_diagnostics(mcp: FastMCP) -> None:
    """Print configuration status and registered tools, then exit.

    Verifies an install without connecting a client. Credential values are never
    printed, only whether they are set.
    """
    from . import __version__

    settings = load_settings()
    tools = asyncio.run(mcp.list_tools())

    print(f"workday-advanced-mcp {__version__}")
    if settings.host and settings.tenant:
        print(f"Endpoint: {settings.endpoint_base}/<service>/{settings.api_version}")
    else:
        print(f"Endpoint: NOT SET (set {ENV_HOST} and {ENV_TENANT})")
    print(f"Auth mode: {settings.auth_mode}")
    print(
        "Credentials: "
        + ("configured" if settings.credentials_configured else "NOT SET")
    )
    print(f"Read-only mode: {'on' if settings.read_only else 'off'}")
    print(
        "Auto-complete: "
        + (
            "permitted"
            if settings.allow_auto_complete
            else f"refused (set {ENV_ALLOW_AUTO_COMPLETE})"
        )
    )
    if settings.enabled_tools:
        print(f"Tool allowlist: {ENV_ENABLED_TOOLS} is set")
    if settings.proxy_url:
        print("Proxy: configured")
    print(f"Registered tools ({len(tools)}):")
    for tool in sorted(tools, key=lambda t: t.name):
        print(f"  - {tool.name}")


def main() -> None:
    """Console entry point."""
    parser = argparse.ArgumentParser(
        prog="workday-advanced-mcp",
        description=(
            "MCP server for Workday Web Services staffing and payment-election writes. "
            f"Reads {ENV_HOST}, {ENV_TENANT} and credentials from the environment; see "
            "the README for the full list."
        ),
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Print configuration status and registered tools, then exit.",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help=f"Register only the read tools, as {ENV_READ_ONLY} does.",
    )
    args = parser.parse_args()

    mcp = build_server(read_only=True if args.read_only else None)

    if args.test:
        _print_diagnostics(mcp)
        return

    mcp.run()


if __name__ == "__main__":
    main()
