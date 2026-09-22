"""Tool registration, gated by deployment configuration.

Read-only mode and the enabled-tools allowlist withhold a tool entirely rather
than refusing it at call time: a tool the deployment has not licensed should not
appear in the agent's prompt at all.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .errors import format_exception


def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Return tool errors as a caller-facing message, never a traceback."""
    import functools

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            return format_exception(exc)

    return wrapper


class ToolRegistrar:
    """Registers tools on a FastMCP server, honouring the allowlist."""

    def __init__(self, mcp: FastMCP, enabled: frozenset[str] | None = None) -> None:
        self._mcp = mcp
        self._enabled = enabled
        self.registered: list[str] = []

    def allows(self, name: str) -> bool:
        return self._enabled is None or name in self._enabled

    def add(
        self,
        fn: Callable[..., Any],
        *,
        name: str,
        title: str,
        description: str,
        read_only: bool,
        destructive: bool = False,
        idempotent: bool = False,
    ) -> None:
        if not self.allows(name):
            return
        annotations = ToolAnnotations(
            title=title,
            readOnlyHint=read_only,
            destructiveHint=destructive,
            idempotentHint=idempotent,
            openWorldHint=True,
        )
        self._mcp.tool(
            name=name,
            title=title,
            description=description,
            annotations=annotations,
        )(guarded(fn))
        self.registered.append(name)
