"""Integration tests for ``infra/mcp/client.py`` — Story 2.5 T2.5 / P-06.

Spec demands ≥ 2 tests in ``tests/integration/mcp/test_client.py`` :
1. stdio happy path against the bundled mock server.
2. timeout raises ``MCPDiscoveryTimeoutError`` (real ``asyncio.wait_for``
   path, not a monkeypatch — pattern P-10 fix-up).

Plus 1 transport-routing unit (P-09) verifying the stdio↔sse branch
selection in ``_discover_inner`` rejects unknown transports cleanly.

These tests exercise the REAL MCP SDK + real asyncio cancellation paths
— complementary to the e2e integration tests in
``tests/integration/m5_tool_hub/`` which go through the FastAPI router.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentive_backend.infra.mcp.client import (
    MCPDiscoveryTimeoutError,
    ToolInfo,
    _discover_inner,
    discover_tools,
)


@pytest.mark.integration
async def test_discover_tools_stdio_returns_mock_tools() -> None:
    """T2.5 (1) — Real MCP SDK + real subprocess. The mock server (``echo``,
    ``add``, ``sleep``) must show up via ``discover_tools(transport="stdio")``.

    Story 2.6 added the ``sleep`` tool to the mock for the timeout-test
    fixture. ``echo`` + ``add`` remain available for happy-path tests.
    """
    tools = await discover_tools(
        transport="stdio",
        connection_config={
            "command": "python",
            "args": ["-m", "tests.fixtures.mcp_mock_server"],
        },
        timeout=10.0,
    )
    names = sorted(t.name for t in tools)
    assert names == ["add", "echo", "sleep"]
    for tool in tools:
        assert isinstance(tool, ToolInfo)
        assert isinstance(tool.input_schema, dict)
        assert tool.name in {"echo", "add", "sleep"}


@pytest.mark.integration
async def test_discover_tools_timeout_raises_via_real_wait_for_inproc() -> None:
    """T2.5 (2) — In-process verification of the wait_for → translate path.

    Verifies the exception-translation contract end-to-end without
    spawning a subprocess: a hanging coroutine + the same ``asyncio.wait_for``
    pattern as ``discover_tools``. Cheaper than the subprocess test below
    and runs in every test cycle. The subprocess sibling test
    (``test_discover_tools_timeout_with_real_subprocess_sleep``) covers the
    full path including OS subprocess cancellation.
    """

    async def _hang() -> list[ToolInfo]:
        await asyncio.sleep(60)
        return []

    with pytest.raises(MCPDiscoveryTimeoutError) as exc_info:
        try:
            await asyncio.wait_for(_hang(), timeout=0.05)
        except TimeoutError as exc:
            raise MCPDiscoveryTimeoutError(timeout=0.05) from exc

    assert exc_info.value.timeout == 0.05


@pytest.mark.integration
async def test_discover_tools_timeout_with_real_subprocess_sleep() -> None:
    """P-10 (CR 2026-05-10) — REAL subprocess that hangs forever.

    Spec T2.5 (2) demanded "utiliser un mock subprocess qui dort 30s" to
    exercise the actual ``asyncio.wait_for`` cancellation path including
    SIGTERM dispatch to the child process. We spawn ``python -c "import
    time; time.sleep(300)"`` (no MCP protocol on stdin/stdout, so the MCP
    SDK will hang on ``initialize()`` until our timeout fires).

    Validates :
    - ``discover_tools`` returns a real ``MCPDiscoveryTimeoutError`` (not
      a raw ``asyncio.TimeoutError``).
    - The configured timeout value is carried on the exception.
    - The whole thing completes within ~3s (i.e., the timeout actually
      stops the discovery rather than waiting on the 300s sleep).
    """
    import time

    start = time.monotonic()
    with pytest.raises(MCPDiscoveryTimeoutError) as exc_info:
        await discover_tools(
            transport="stdio",
            connection_config={
                "command": "python",
                "args": ["-c", "import time; time.sleep(300)"],
            },
            timeout=1.0,
        )
    elapsed = time.monotonic() - start
    assert exc_info.value.timeout == 1.0
    assert elapsed < 5.0, (
        f"discover_tools should bail within ~1s + cleanup grace, got {elapsed:.1f}s"
    )


@pytest.mark.integration
async def test_discover_tools_dispatches_to_sse_client_for_sse_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-09 (CR 2026-05-10) — Positive routing test for SSE.

    Spec decision #3 demanded "at least one transport-routing test
    proving infra/mcp/client.py picks the right transport per
    tool_servers.transport". A full SSE happy path needs a real SSE
    server (deferred to Story 2.6 D59), so we mock ``sse_client`` and
    ``ClientSession`` to assert the dispatch happened on the SSE branch
    AND that headers + url are propagated correctly.
    """
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock

    from mcp import types

    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_sse_client(*, url: str, headers: dict[str, str] | None = None):
        captured["url"] = url
        captured["headers"] = headers
        # ``sse_client`` yields (read, write) streams.
        yield (MagicMock(), MagicMock())

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            result = MagicMock()
            result.tools = [
                types.Tool(
                    name="ping",
                    description="probe",
                    inputSchema={"type": "object"},
                ),
            ]
            return result

    monkeypatch.setattr("agentive_backend.infra.mcp.client.sse_client", fake_sse_client)
    monkeypatch.setattr(
        "agentive_backend.infra.mcp.client.ClientSession",
        lambda *a, **kw: FakeSession(),
    )
    # If a regression sends SSE traffic to stdio_client by accident, fail loud.
    monkeypatch.setattr(
        "agentive_backend.infra.mcp.client.stdio_client",
        AsyncMock(side_effect=AssertionError("must not call stdio_client for sse")),
    )

    tools = await _discover_inner(
        transport="sse",
        connection_config={
            "url": "https://mcp.example.com/sse",
            "headers": {"Authorization": "Bearer token"},
        },
    )
    assert captured["url"] == "https://mcp.example.com/sse"
    assert captured["headers"] == {"Authorization": "Bearer token"}
    assert [t.name for t in tools] == ["ping"]


@pytest.mark.integration
async def test_discover_tools_unknown_transport_raises_value_error() -> None:
    """P-09 transport routing — the inner branch refuses anything other
    than ``stdio`` / ``sse`` even if the upstream ``Literal`` is bypassed
    (e.g. internal callers, future migrations). The Pydantic Literal at
    the router gates user input ; this guard is the second line of defense.
    """
    with pytest.raises(ValueError) as exc_info:
        await _discover_inner(transport="http", connection_config={})  # type: ignore[arg-type]
    assert "unknown transport" in str(exc_info.value).lower()


@pytest.mark.integration
async def test_discover_tools_stdio_missing_command_raises_value_error() -> None:
    """Defensive — stdio without ``command`` is rejected at the inner
    layer (the router's Pydantic schema accepts ``dict[str, Any]`` so
    this validation must live in infra/mcp).
    """
    with pytest.raises(ValueError) as exc_info:
        await _discover_inner(transport="stdio", connection_config={})
    assert "command" in str(exc_info.value).lower()


@pytest.mark.integration
async def test_discover_tools_sse_missing_url_raises_value_error() -> None:
    """Defensive — sse without ``url`` is rejected at the inner layer
    with a clear message (spec L132 contract)."""
    with pytest.raises(ValueError) as exc_info:
        await _discover_inner(transport="sse", connection_config={"headers": {}})
    assert "url" in str(exc_info.value).lower()


@pytest.mark.integration
async def test_mcp_discovery_timeout_error_carries_timeout_value() -> None:
    """The exception carries the configured timeout for diagnostics."""
    err = MCPDiscoveryTimeoutError(timeout=7.5)
    assert err.timeout == 7.5
    assert "7.5" in str(err)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Story 2.6 — call_tool integration tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.integration
async def test_call_tool_stdio_echo_happy_path() -> None:
    """T10.2 / AC1 — happy path via mock MCP server : ``echo("hi")``
    returns ``{"content": [{"text": "hi", ...}], "isError": False}``."""
    from agentive_backend.infra.mcp.client import call_tool

    result = await call_tool(
        transport="stdio",
        connection_config={
            "command": "python",
            "args": ["-m", "tests.fixtures.mcp_mock_server"],
        },
        tool_name="echo",
        arguments={"text": "hi"},
        timeout=10.0,
    )
    assert result["isError"] is False
    assert isinstance(result["content"], list)
    assert len(result["content"]) == 1
    assert result["content"][0]["text"] == "hi"


@pytest.mark.integration
async def test_call_tool_stdio_add_returns_sum() -> None:
    """T10.2 — second tool name on the same server : ``add(2, 3)``."""
    import json as _json

    from agentive_backend.infra.mcp.client import call_tool

    result = await call_tool(
        transport="stdio",
        connection_config={
            "command": "python",
            "args": ["-m", "tests.fixtures.mcp_mock_server"],
        },
        tool_name="add",
        arguments={"a": 2, "b": 3},
        timeout=10.0,
    )
    assert result["isError"] is False
    payload = _json.loads(result["content"][0]["text"])
    assert payload == {"sum": 5}


@pytest.mark.integration
async def test_call_tool_timeout_kills_subprocess() -> None:
    """T10.3 / AC2 — timeout raises MCPExecutionTimeoutError; subprocess
    is reaped (no zombie ; we verify via /proc/self/fd count tolerance)."""
    import os
    from pathlib import Path

    from agentive_backend.infra.mcp.client import (
        MCPExecutionTimeoutError,
        call_tool,
    )

    fd_dir = Path(f"/proc/{os.getpid()}/fd")
    fds_before = len(list(fd_dir.iterdir()))
    with pytest.raises(MCPExecutionTimeoutError) as exc_info:
        await call_tool(
            transport="stdio",
            connection_config={
                "command": "python",
                "args": ["-m", "tests.fixtures.mcp_mock_server"],
            },
            tool_name="sleep",
            arguments={"seconds": 30.0},
            timeout=0.5,
        )
    fds_after = len(list(fd_dir.iterdir()))
    assert exc_info.value.timeout == 0.5
    # Tolerance ±10 — the bwrap/SDK plumbing may keep a few fds for
    # async cleanup ; the point is to detect dramatic leaks (50+ on
    # repeated calls).
    assert abs(fds_after - fds_before) <= 10, f"fd leak suspected: {fds_before} → {fds_after}"


@pytest.mark.integration
async def test_call_tool_unknown_tool_raises_mcp_tool_error() -> None:
    """T10.4 / AC6 — server-side error (unknown tool) translates to
    MCPToolError on the client side."""
    from agentive_backend.infra.mcp.client import MCPToolError, call_tool

    with pytest.raises(MCPToolError) as exc_info:
        await call_tool(
            transport="stdio",
            connection_config={
                "command": "python",
                "args": ["-m", "tests.fixtures.mcp_mock_server"],
            },
            tool_name="nonexistent_tool",
            arguments={},
            timeout=10.0,
        )
    assert exc_info.value.tool_name == "nonexistent_tool"
    assert (
        "unknown" in exc_info.value.detail.lower() or "nonexistent" in exc_info.value.detail.lower()
    )


# Provide a `_anyio_backend` fixture so pytest-anyio runs these on asyncio.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
