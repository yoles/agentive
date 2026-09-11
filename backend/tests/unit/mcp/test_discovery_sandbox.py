"""Unit tests — ``discover_tools`` spawns its stdio subprocess SANDBOXED
(review IG2).

Story 2.6 wrapped ``call_tool`` in bwrap (or the setrlimit fallback) but left
``discover_tools`` spawning the user-supplied ``command`` unconfined. That is
the same RCE surface ``AGENTIVE_ALLOW_MCP_REGISTRATION`` gates registration
behind — and Story 4.5's pre-workflow check calls discovery on every
``POST /workflows/{id}/runs``, i.e. from an endpoint with no such gate.

These tests assert on the ``StdioServerParameters`` handed to the MCP SDK,
which is where the confinement decision becomes observable without needing a
real bwrap binary (absent from CI, cf. the skips in
``tests/integration/mcp/test_sandbox_bypass.py``).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

import agentive_backend.infra.mcp.client as client_module
from agentive_backend.infra.mcp.client import discover_tools
from agentive_backend.infra.mcp.sandbox import SandboxProfile


@pytest.fixture
def captured_params(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Capture the ``StdioServerParameters`` the SDK would be spawned with,
    and short-circuit the session so no subprocess is ever created."""
    captured: list[Any] = []

    class _FakeStdioClient:
        def __init__(self, params: Any) -> None:
            captured.append(params)

        async def __aenter__(self) -> tuple[Any, Any]:
            return MagicMock(), MagicMock()

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

    async def _fake_list_tools(_read: Any, _write: Any) -> list[Any]:
        return []

    monkeypatch.setattr(client_module, "stdio_client", _FakeStdioClient)
    monkeypatch.setattr(client_module, "_list_tools_via_session", _fake_list_tools)
    return captured


@pytest.mark.asyncio
async def test_discover_tools_when_stdio_should_wrap_the_command_in_bwrap(
    captured_params: list[Any],
) -> None:
    """The SDK must be asked to launch `bwrap`, with the real command
    demoted to a tail argument — never the command directly."""
    await discover_tools(
        transport="stdio",
        connection_config={"command": "my-server", "args": ["--port", "1"]},
        backend="bwrap",
    )

    params = captured_params[0]
    assert params.command == "bwrap"
    # The real command survives, but only inside the sandbox's argv.
    assert "my-server" in params.args
    assert params.args.index("my-server") < params.args.index("--port")


@pytest.mark.asyncio
async def test_discover_tools_when_setrlimit_fallback_should_still_wrap(
    captured_params: list[Any],
) -> None:
    """No bwrap binary is not a licence to spawn unconfined — the setrlimit
    bootstrap wraps the command instead."""
    await discover_tools(
        transport="stdio",
        connection_config={"command": "my-server", "args": []},
        backend="setrlimit",
    )

    params = captured_params[0]
    assert params.command != "my-server"
    assert "my-server" in params.args


@pytest.mark.asyncio
async def test_discover_tools_should_filter_env_through_the_profile(
    captured_params: list[Any],
) -> None:
    """An unfiltered `connection_config["env"]` leaked secret-bearing keys
    into the subprocess — `call_tool` already filtered, discovery did not."""
    profile = SandboxProfile()
    passthrough = next(iter(profile.env_passthrough))

    await discover_tools(
        transport="stdio",
        connection_config={
            "command": "my-server",
            "args": [],
            "env": {passthrough: "kept", "ANTHROPIC_API_KEY": "sk-ant-leaked"},
        },
        backend="setrlimit",
        profile=profile,
    )

    params = captured_params[0]
    assert params.env == {passthrough: "kept"}
    assert "ANTHROPIC_API_KEY" not in (params.env or {})


@pytest.mark.asyncio
async def test_discover_tools_when_sse_should_not_be_sandboxed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE spawns no subprocess — there is nothing local to confine, and
    `call_tool` makes the same exemption."""
    seen: list[dict[str, Any]] = []

    class _FakeSseClient:
        def __init__(self, *, url: str, headers: Any = None) -> None:
            seen.append({"url": url, "headers": headers})

        async def __aenter__(self) -> tuple[Any, Any]:
            return MagicMock(), MagicMock()

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

    async def _fake_list_tools(_read: Any, _write: Any) -> list[Any]:
        return []

    monkeypatch.setattr(client_module, "sse_client", _FakeSseClient)
    monkeypatch.setattr(client_module, "_list_tools_via_session", _fake_list_tools)

    await discover_tools(transport="sse", connection_config={"url": "https://mcp.example.test/sse"})

    assert seen == [{"url": "https://mcp.example.test/sse", "headers": None}]
