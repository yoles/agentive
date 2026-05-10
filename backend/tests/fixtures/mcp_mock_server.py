"""Minimal MCP mock server (stdio) — Story 2.5 T10.2 fixture.

Runnable via ``python -m tests.fixtures.mcp_mock_server``. Exposes 2
trivial tools (``echo`` + ``add``) for the integration tests of
``ToolHubService.connect_server`` (AC1 stdio happy path + atomicity test).

Implementation : ``mcp.server.lowlevel.Server`` + ``stdio_server`` from
the official Python SDK. ~50 LOC. No Node.js dependency in CI.

Anti-scope :
- No call_tool implementation (Story 2.6 sandbox runtime).
- Only stdio transport (SSE mock défer Story 2.6 OR transport routing
  unit test in T7.5).
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def build_server() -> Server:
    """Build a Server instance exposing 2 trivial tools."""
    server: Server[Any, Any] = Server(name="mcp-mock-server", version="0.0.1")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="echo",
                description="Echo the input string back",
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            ),
            types.Tool(
                name="add",
                description="Add two integers and return the sum",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "a": {"type": "integer"},
                        "b": {"type": "integer"},
                    },
                    "required": ["a", "b"],
                },
            ),
        ]

    return server


async def main() -> None:
    """Run the mock server forever, pumping JSON-RPC over stdio."""
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":  # pragma: no cover — invoked as a subprocess by tests
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
