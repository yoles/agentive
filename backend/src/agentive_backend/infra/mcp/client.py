"""MCP client — thin wrapper sur le SDK Python ``mcp>=1.27.0``.

Story 2.5 — discovery (connexion éphémère + ``list_tools()``).
Story 2.6 — runtime execution (``call_tool`` via sandbox bwrap + fallback
setrlimit, voir :mod:`agentive_backend.infra.mcp.sandbox`).

API publique :
- :func:`discover_tools` (Story 2.5) — `list_tools()` only.
- :func:`call_tool` (Story 2.6) — invoke one tool with arguments, returns
  the result dict. Sandbox applied to stdio subprocess ; SSE bypass
  réseau (le serveur est distant — voir Dev Notes Story 2.6 décision #3).

Toutes les erreurs infra-level (timeout, crash subprocess, tool error)
sont traduites en domain ``DependencyError`` / ``NotFoundError`` par les
features/ (P-08 Story 2.5 pattern, features/ ne connaît jamais les
exceptions infra/MCP — Story 2.1 P-01 import-linter Contract 3 friendly).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Final, Literal

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client

from agentive_backend.infra.mcp.sandbox import (
    MCPExecutionError,
    MCPExecutionTimeoutError,
    MCPToolError,
    SandboxBackend,
    SandboxProfile,
)

# Audit M-01 (1.4/3.1.5) — single source of truth for MCP timeout budgets.
# Callers (tool_hub service) import these instead of re-literalizing
# 10.0 / 30.0, so the values cannot silently diverge between layers.
DEFAULT_DISCOVERY_TIMEOUT_S: Final = 10.0
DEFAULT_INVOKE_TIMEOUT_S: Final = 30.0


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
    timeout: float = DEFAULT_DISCOVERY_TIMEOUT_S,
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
    # `cm` is bound BEFORE the `async with` so the handler below can always
    # read it: bound inside, it exists only if `Timeout.__aenter__` succeeded
    # (CR 2026-09-06, P15).
    cm = asyncio.timeout(timeout)
    try:
        async with cm:
            return await _discover_inner(transport=transport, connection_config=connection_config)
    except TimeoutError as exc:
        raise MCPDiscoveryTimeoutError(timeout=timeout) from exc
    except BaseExceptionGroup as exc_group:
        if not _deadline_won_the_race(expired=cm.expired()):
            raise
        raise MCPDiscoveryTimeoutError(timeout=timeout) from exc_group


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
        # P-17 (CR 2026-05-10) — element-type validation : subprocess.Popen
        # crashes with TypeError on non-str args/env, which then bubbles up as
        # an opaque 500 (or, post-P-08, as a generic DependencyError). Reject
        # early with a clear ValueError so the user sees the actual field at
        # fault.
        if not all(isinstance(arg, str) for arg in args):
            raise ValueError("stdio connection_config 'args' must contain only strings")
        env = connection_config.get("env")
        if env is not None and not isinstance(env, dict):
            raise ValueError("stdio connection_config 'env' must be a dict if present")
        if env is not None and not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise ValueError("stdio connection_config 'env' keys and values must all be strings")
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
        infos: list[ToolInfo] = []
        for tool in result.tools:
            # P-18 (CR 2026-05-10) — reject empty/missing tool names early so
            # a buggy MCP server cannot persist a row with name="" that the
            # UI then renders as a blank checkbox label.
            if not isinstance(tool.name, str) or not tool.name:
                raise ValueError("MCP server returned a tool with empty or missing 'name'")
            # P-16 (CR 2026-05-10) — defensive ``getattr`` : older MCP SDK
            # payloads may omit ``outputSchema`` entirely (attribute missing
            # vs set to None). Without this, AttributeError leaks as 500.
            output_schema_raw = getattr(tool, "outputSchema", None)
            infos.append(
                ToolInfo(
                    name=tool.name,
                    description=tool.description or "",
                    # `inputSchema` is camelCase in the MCP wire format — we
                    # snake_case it for our DB layer (Story 2.5 decision #1).
                    input_schema=dict(tool.inputSchema or {}),
                    output_schema=(dict(output_schema_raw) if output_schema_raw else None),
                )
            )
        return infos


async def call_tool(
    *,
    transport: Literal["stdio", "sse"],
    connection_config: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    timeout: float = DEFAULT_INVOKE_TIMEOUT_S,
    profile: SandboxProfile | None = None,
    backend: SandboxBackend | None = None,
) -> dict[str, Any]:
    """Invoke ``tool_name`` on the MCP server pointed to by
    ``connection_config`` and return its result (Story 2.6 AC1).

    For ``transport="stdio"`` the server subprocess runs INSIDE a
    :func:`agentive_backend.infra.mcp.sandbox.sandboxed_subprocess`
    (bwrap or setrlimit fallback) — no network, read-only filesystem,
    ``/tmp`` tmpfs, capabilities dropped, parent-death signal.

    For ``transport="sse"`` the sandbox does NOT wrap the remote server
    (it's not our process — see Dev Notes Story 2.6 décision #3). The
    HTTP/SSE client traffic uses our backend process directly ;
    setrlimits would apply to the backend itself, which we don't want
    Sprint 1. URL allowlist defer to Story 4.x (D63).

    Parameters
    ----------
    transport, connection_config
        Same shape as :func:`discover_tools`.
    tool_name
        The tool to call. Must match a tool the server exposes.
    arguments
        Tool-specific input dict. MCP server validates against the tool's
        ``inputSchema``.
    timeout
        Wall-clock cap on the entire call (connect + initialize + call +
        close). Exceeding raises :class:`MCPExecutionTimeoutError`.
    profile, backend
        Override sandbox profile / backend. Both ``None`` = production
        defaults.

    Returns
    -------
    dict[str, Any]
        The MCP ``CallToolResult`` shape : ``{"content": [...],
        "isError": bool, ...}``. Caller (workflow_engine Story 4.x or
        playground Story 2.7) decides how to unwrap.

    Raises
    ------
    MCPExecutionTimeoutError
        Call did not complete within ``timeout`` seconds.
    MCPExecutionError
        Subprocess crashed / non-zero exit / SDK protocol error.
    MCPToolError
        Server returned ``CallToolResult.isError=True`` (tool semantic
        rejection — e.g. invalid arguments per the tool's schema).
    ValueError
        ``connection_config`` is malformed (missing required keys for
        the requested transport).
    """
    cm = asyncio.timeout(timeout)
    try:
        async with cm:
            return await _call_tool_inner(
                transport=transport,
                connection_config=connection_config,
                tool_name=tool_name,
                arguments=arguments,
                profile=profile,
                backend=backend,
            )
    except TimeoutError as exc:
        raise MCPExecutionTimeoutError(timeout=timeout) from exc
    except BaseExceptionGroup as exc_group:
        # Same teardown race as `discover_tools` — see `_deadline_won_the_race`.
        if not _deadline_won_the_race(expired=cm.expired()):
            raise
        raise MCPExecutionTimeoutError(timeout=timeout) from exc_group


async def _call_tool_inner(
    *,
    transport: Literal["stdio", "sse"],
    connection_config: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    profile: SandboxProfile | None,
    backend: SandboxBackend | None,
) -> dict[str, Any]:
    """Inner call without the timeout wrapper.

    Dispatches transport to stdio (sandboxed subprocess via
    :func:`agentive_backend.infra.mcp.sandbox.sandboxed_subprocess` + MCP
    SDK ``stdio_client`` on its pipes) or sse (no sandbox — direct
    ``sse_client``).
    """
    if transport == "stdio":
        command = connection_config.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("stdio connection_config requires non-empty 'command'")
        args = connection_config.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError("stdio connection_config 'args' must be a list of strings")
        env = connection_config.get("env")
        if env is not None and not isinstance(env, dict):
            raise ValueError("stdio connection_config 'env' must be a dict if present")

        # Sprint 1 — use the MCP SDK's ``stdio_client`` with
        # :class:`StdioServerParameters` and a wrapped ``command``/``args``
        # that re-routes the spawn through bwrap. Cheap trick : we ask the
        # SDK to spawn ``bwrap`` and pass the original command as bwrap's
        # tail args. The SDK believes it's launching ``bwrap`` (which is
        # true) but bwrap then execs the real command inside the sandbox.
        #
        # P-02 (CR 2026-05-11) — filter ``env`` via
        # ``profile.env_passthrough`` BEFORE forwarding to the SDK. In
        # setrlimit fallback mode the SDK spawns the bootstrap subprocess
        # which inherits this env wholesale ; without filtering, secret-
        # bearing keys from ``connection_config["env"]`` would leak into
        # the subprocess.
        effective_profile = profile or SandboxProfile()
        effective_env: dict[str, str] | None
        if env is not None:
            effective_env = {k: v for k, v in env.items() if k in effective_profile.env_passthrough}
        else:
            effective_env = None
        sandbox_argv_prefix = _sandbox_argv_prefix(
            profile=effective_profile, backend=backend, parent_env=effective_env
        )
        if sandbox_argv_prefix:
            wrapped_command = sandbox_argv_prefix[0]
            wrapped_args = [*sandbox_argv_prefix[1:], command, *args]
        else:
            wrapped_command = command
            wrapped_args = list(args)

        params = StdioServerParameters(
            command=wrapped_command, args=wrapped_args, env=effective_env
        )
        try:
            async with stdio_client(params) as (read, write):
                return await _call_tool_via_session(read, write, tool_name, arguments)
        except BaseExceptionGroup as exc_group:
            # anyio.create_task_group (used by ClientSession + stdio_client)
            # wraps inner exceptions in BaseExceptionGroup. Unwrap so the
            # caller can ``pytest.raises(MCPToolError, ...)`` as documented.
            _reraise_domain_error_from_group(exc_group)
            raise
        except OSError as exc:
            raise MCPExecutionError(returncode=-1, stderr_tail=str(exc)) from exc
    elif transport == "sse":
        url = connection_config.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("sse connection_config requires non-empty 'url'")
        headers = connection_config.get("headers")
        if headers is not None and not isinstance(headers, dict):
            raise ValueError("sse connection_config 'headers' must be a dict if present")
        async with sse_client(url=url, headers=headers) as (read, write):
            return await _call_tool_via_session(read, write, tool_name, arguments)
    else:
        raise ValueError(f"Unknown transport: {transport!r}")


def _deadline_won_the_race(*, expired: bool) -> bool:
    """True iff an exception group escaping the timeout block is the teardown
    debris of OUR own deadline, and may therefore be reported as a timeout.

    Under the setrlimit sandbox fallback, cancelling at the deadline races the
    ``stdio_client`` teardown: its background reader task pushes onto an
    already closed stream and raises ``anyio.BrokenResourceError``, so what
    reaches the caller is an exception GROUP, not the bare ``CancelledError``
    that ``asyncio.timeout`` knows how to convert into ``TimeoutError``.
    ``expired()`` still reports the deadline was hit, whichever exception won
    that race — hence this shortcut.

    Two guards keep the shortcut from swallowing something real
    (CR 2026-09-06, P15):

    - the deadline must actually have fired ;
    - a cancellation still pending on this task wins over it. Withdrawing its
      own request is the FIRST thing ``asyncio.timeout.__aexit__`` does, so a
      non-zero ``Task.cancelling()`` here can only come from somebody else (an
      enclosing timeout, a shutdown). Reporting a tool timeout in that case
      converts a cooperative cancellation into a domain error and the caller
      never stops.

    The group itself is chained onto the raised error (``from exc_group``, not
    the previous ``from None``), so an unrelated failure that shared the race
    stays readable in the traceback instead of vanishing.
    """
    if not expired:
        return False
    task = asyncio.current_task()
    return task is None or task.cancelling() == 0


def _reraise_domain_error_from_group(exc_group: BaseExceptionGroup) -> None:
    """If ``exc_group`` (recursively) contains a domain error
    (:class:`MCPToolError`, :class:`MCPExecutionError`,
    :class:`MCPExecutionTimeoutError`), re-raise that inner exception so
    the caller sees the original domain class rather than the anyio
    ``BaseExceptionGroup`` wrapping it.

    P-07 (CR 2026-05-11) — also unwraps ``OSError`` and translates it to
    :class:`MCPExecutionError`. anyio task groups wrap subprocess-spawn
    errors (ENOENT on missing ``bwrap`` binary, EACCES on permission
    denied, etc.) ; without this branch the OSError leaks as a
    ``BaseExceptionGroup`` past ``service.invoke_tool``'s ``except
    (MCPExecutionTimeoutError | MCPToolError | MCPExecutionError)`` and
    surfaces as a generic 500 instead of the canonical 503.

    Returns silently if no relevant exception is found ; the caller then
    re-raises the original group.
    """
    domain_types = (MCPToolError, MCPExecutionError, MCPExecutionTimeoutError)
    for exc in exc_group.exceptions:
        if isinstance(exc, domain_types):
            raise exc from exc_group
        if isinstance(exc, OSError):
            raise MCPExecutionError(
                returncode=-1, stderr_tail=f"{type(exc).__name__}: {exc}"
            ) from exc
        if isinstance(exc, BaseExceptionGroup):
            _reraise_domain_error_from_group(exc)


def _sandbox_argv_prefix(
    *,
    profile: SandboxProfile | None,
    backend: SandboxBackend | None,
    parent_env: dict[str, str] | None = None,
) -> list[str]:
    """Compute the bwrap (or setrlimit bootstrap) argv prefix to wrap the
    target command.

    P-09 (CR 2026-05-11) — placeholder validation hardened against ``python -O``
    (which strips ``assert``). Use explicit ``RuntimeError`` instead.
    """
    from agentive_backend.infra.mcp.sandbox import (
        _build_bwrap_argv,
        _build_setrlimit_bootstrap,
        detect_sandbox_backend,
    )

    effective_backend = backend or detect_sandbox_backend()
    effective_profile = profile or SandboxProfile()

    if effective_backend == "bwrap":
        # Build a bwrap argv that ends with ``--`` and an empty trailing
        # command — we'll splice the real command in afterward.
        argv = _build_bwrap_argv(
            "__PLACEHOLDER__",
            [],
            profile=effective_profile,
            parent_env=parent_env,
        )
        # Drop the placeholder so callers can append the real command.
        if argv[-1] != "__PLACEHOLDER__":
            raise RuntimeError(
                f"_build_bwrap_argv layout drift: trailing element is {argv[-1]!r}, "
                "not the expected placeholder. Refusing to strip."
            )
        return argv[:-1]
    else:
        # setrlimit bootstrap is ``python -c "..." <command> <args...>`` — we
        # return the python bootstrap (sans command) and let the caller append.
        bootstrap = _build_setrlimit_bootstrap("__PLACEHOLDER__", [], profile=effective_profile)
        if bootstrap[-1] != "__PLACEHOLDER__":
            raise RuntimeError(
                f"_build_setrlimit_bootstrap layout drift: trailing element is "
                f"{bootstrap[-1]!r}, not the expected placeholder. Refusing to strip."
            )
        return bootstrap[:-1]


async def _call_tool_via_session(
    read: Any,
    write: Any,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Open a ``ClientSession``, initialize, call ``tool_name``, map the
    result to a plain dict.

    Translates ``CallToolResult.isError=True`` into :class:`MCPToolError`.
    """
    async with ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool(tool_name, arguments=arguments)

        # Convert content blocks to plain dicts for the audit/payload layer.
        content_dicts: list[dict[str, Any]] = []
        for block in result.content:
            if hasattr(block, "model_dump"):
                content_dicts.append(block.model_dump())
            else:  # pragma: no cover — defensive for older SDK shapes
                content_dicts.append(dict(block))

        if result.isError:
            # Extract a human-readable error message from the first text block.
            detail = "tool returned error"
            for block_dict in content_dicts:
                text_value = block_dict.get("text")
                if isinstance(text_value, str):
                    detail = text_value
                    break
            raise MCPToolError(tool_name=tool_name, detail=detail)

        return {"content": content_dicts, "isError": False}


__all__ = [
    "DEFAULT_DISCOVERY_TIMEOUT_S",
    "DEFAULT_INVOKE_TIMEOUT_S",
    "MCPDiscoveryTimeoutError",
    "MCPExecutionError",
    "MCPExecutionTimeoutError",
    "MCPToolError",
    "ToolInfo",
    "call_tool",
    "discover_tools",
]
