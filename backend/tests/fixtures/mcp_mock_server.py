"""Minimal MCP mock server (stdio) — Stories 2.5 + 2.6 fixture.

Runnable via ``python -m tests.fixtures.mcp_mock_server``. Exposes 3
trivial tools (``echo``, ``add``, ``sleep``) for the integration tests of
:class:`ToolHubService.connect_server` (Story 2.5 AC1) AND
:func:`...client.call_tool` / :meth:`ToolHubService.invoke_tool`
(Story 2.6 AC1 + AC2 timeout test).

Implementation : ``mcp.server.lowlevel.Server`` + ``stdio_server`` from
the official Python SDK. No Node.js dependency in CI.

Tools :
- ``echo(text: str)`` — returns ``text`` verbatim. Used for happy path.
- ``add(a: int, b: int)`` — returns ``{"sum": a + b}`` in JSON. Used to
  exercise a 2nd tool name on the same server.
- ``sleep(seconds: float)`` — blocks for ``seconds`` then returns
  ``"done"``. Used for the Story 2.6 timeout test (call with large
  ``seconds`` + tiny ``timeout_seconds`` → ``MCPExecutionTimeoutError``).
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def build_server() -> Server:
    """Build a Server instance exposing 3 trivial tools."""
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
            types.Tool(
                name="sleep",
                description="Block for `seconds` then return 'done' (timeout test fixture)",
                inputSchema={
                    "type": "object",
                    "properties": {"seconds": {"type": "number"}},
                    "required": ["seconds"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        """Dispatch tool calls — Story 2.6 AC1 fixture.

        Returns a list of ``TextContent`` blocks (MCP wire format).
        Raises for unknown tool names → SDK translates to
        ``CallToolResult.isError=True`` which our infra/mcp/client.py
        maps to :class:`MCPToolError` (NotFoundError 404 at service layer).
        """
        if name == "echo":
            return [types.TextContent(type="text", text=str(arguments.get("text", "")))]
        if name == "add":
            a = int(arguments.get("a", 0))
            b = int(arguments.get("b", 0))
            return [
                types.TextContent(type="text", text=json.dumps({"sum": a + b})),
            ]
        if name == "sleep":
            seconds = float(arguments.get("seconds", 0))
            await asyncio.sleep(seconds)
            return [types.TextContent(type="text", text="done")]
        raise ValueError(f"unknown tool: {name}")

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
