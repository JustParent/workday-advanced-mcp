"""MCP server for Workday staffing and payment-election writes."""

from .server import build_server, main

__version__ = "0.1.0"

__all__ = ["__version__", "build_server", "main"]
