"""MCP discovery client — thin wrapper sur le SDK Python ``mcp>=1.27.0``.

Story 2.5 — Sprint 1 livre uniquement la **discovery** (connexion éphémère
+ ``list_tools()``) ; l'**exécution** runtime des outils (call MCP réel via
sandbox bwrap) arrive Story 2.6.

API publique : :func:`discover_tools` ouvre une connexion vers le serveur
MCP via le transport demandé (stdio | sse), récupère la liste de ses outils,
ferme la connexion. Tout est wrappé dans ``asyncio.wait_for(timeout=10s)``
pour qu'un serveur hangulé ne bloque pas le request handler FastAPI.

Domain error :class:`MCPDiscoveryTimeoutError` est traduite en
:class:`agentive_backend.shared.exceptions.DependencyError` par le service
(features/ ne doit jamais connaître les exceptions infra/MCP — Story 2.1
P-01 import-linter Contract 3 friendly).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client


@dataclass(frozen=True)
class ToolInfo:
    """A single tool discovered from a remote MCP server.

    Mirrors a subset of ``mcp.types.Tool`` — we only persist what the
    Story 2.5 schema needs (``name``, ``description``, ``inputSchema``,
    ``outputSchema``). The MCP SDK exposes more (``icons``, ``annotations``,
    ``meta``, ``execution``) — Sprint 1 ignores these (defer Story 2.7+).
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None


class MCPDiscoveryTimeoutError(Exception):
    """Raised by :func:`discover_tools` when the MCP discovery exceeds the
    timeout budget (default 10s).

    The service layer translates this into a domain
    :class:`agentive_backend.shared.exceptions.DependencyError` so that
    ``features/`` stays free of MCP/infra exception types.
    """

    def __init__(self, *, timeout: float) -> None:
        super().__init__(f"MCP discovery timed out after {timeout}s")
        self.timeout = timeout


async def discover_tools(
    *,
    transport: Literal["stdio", "sse"],
    connection_config: dict[str, Any],
    timeout: float = 10.0,
) -> list[ToolInfo]:
    """Discover the tools exposed by a remote MCP server.

    Connects via ``transport`` (stdio or sse), initializes a
    :class:`mcp.ClientSession`, calls ``list_tools()``, and closes the
    connection. Sprint 1 = ephemeral connection (no persistent pool ;
    deferred to Story 2.6).

    Parameters
    ----------
    transport
        ``"stdio"`` (subprocess via stdin/stdout JSON-RPC) or
        ``"sse"`` (HTTP Server-Sent Events).
    connection_config
        For ``stdio`` : ``{"command": str, "args": list[str], "env"?: dict}``.
        For ``sse`` : ``{"url": str, "headers"?: dict[str, str]}``.
    timeout
        Hard wall-clock cap on the entire discovery (connect + initialize +
        list_tools + close). Default 10s. Exceeding the cap raises
        :class:`MCPDiscoveryTimeoutError`.

    Returns
    -------
    list[ToolInfo]
        The tools the server exposes. Empty list is valid (server with no
        tools).

    Raises
    ------
    MCPDiscoveryTimeoutError
        Discovery did not complete within ``timeout`` seconds.
    ValueError
        ``connection_config`` is missing required keys for the requested
        transport (defensive — the FastAPI route should reject malformed
        bodies via Pydantic before reaching this point).
    """
    try:
        return await asyncio.wait_for(
            _discover_inner(transport=transport, connection_config=connection_config),
            timeout=timeout,
        )
    except TimeoutError as exc:
        raise MCPDiscoveryTimeoutError(timeout=timeout) from exc


async def _discover_inner(
    *,
    transport: Literal["stdio", "sse"],
    connection_config: dict[str, Any],
) -> list[ToolInfo]:
    """Inner discovery loop without the timeout wrapper. Kept separate so
    the wait_for stack-trace points to a clean signature."""
    if transport == "stdio":
        command = connection_config.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("stdio connection_config requires non-empty 'command'")
        args = connection_config.get("args", [])
        if not isinstance(args, list):
            raise ValueError("stdio connection_config 'args' must be a list")
        env = connection_config.get("env")
        if env is not None and not isinstance(env, dict):
            raise ValueError("stdio connection_config 'env' must be a dict if present")
        params = StdioServerParameters(command=command, args=list(args), env=env)
        async with stdio_client(params) as (read, write):
            return await _list_tools_via_session(read, write)
    elif transport == "sse":
        url = connection_config.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("sse connection_config requires non-empty 'url'")
        headers = connection_config.get("headers")
        if headers is not None and not isinstance(headers, dict):
            raise ValueError("sse connection_config 'headers' must be a dict if present")
        # Forward headers iff present (sse_client's signature accepts None).
        async with sse_client(url=url, headers=headers) as (read, write):
            return await _list_tools_via_session(read, write)
    else:
        # Defensive — the Pydantic Literal["stdio", "sse"] should reject this
        # earlier, but keep a clear error if we ever extend the type.
        raise ValueError(f"Unknown transport: {transport!r}")


async def _list_tools_via_session(read: Any, write: Any) -> list[ToolInfo]:
    """Open a ClientSession on the (read, write) streams, initialize, list
    tools, and map them to :class:`ToolInfo` instances."""
    async with ClientSession(read, write) as session:
        await session.initialize()
        result = await session.list_tools()
        return [
            ToolInfo(
                name=tool.name,
                description=tool.description or "",
                # `inputSchema` is camelCase in the MCP wire format — we
                # snake_case it for our DB layer (Story 2.5 décision #1).
                input_schema=dict(tool.inputSchema or {}),
                output_schema=(dict(tool.outputSchema) if tool.outputSchema else None),
            )
            for tool in result.tools
        ]


__all__ = ["MCPDiscoveryTimeoutError", "ToolInfo", "discover_tools"]
