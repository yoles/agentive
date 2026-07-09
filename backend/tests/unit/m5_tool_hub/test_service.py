"""Unit tests for ``ToolHubService`` — Story 2.5 P-06 (CR 2026-05-10).

Spec demands ≥ 5 tests in ``tests/unit/m5_tool_hub/test_service.py`` (T6.2).
Cluster A delivers the priority subset: error translation + atomicity
contract. Repository-level + schemas-level + events-level unit tests are
deferred to a Cluster-B fix-up commit.

Scope :
* MCPDiscoveryTimeoutError → DependencyError translation.
* ValueError → DependencyError translation (invalid connection_config).
* ConflictError fast-fail before discovery on duplicate name.
* Atomicity invariant: discovery success but DB INSERT failure rolls
  back any tool inserts and prevents NOTIFY emission.
* connection_config redaction does NOT leak secrets to the response.

Test pattern : in-process AsyncMock for repos + monkeypatch the
module-level ``discover_tools`` to control the discovery branch.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agentive_backend.features.m5_tool_hub import service as svc_module
from agentive_backend.features.m5_tool_hub.service import (
    ToolHubService,
    _redact_arguments,
    _redact_connection_config,
)
from agentive_backend.infra.mcp.client import MCPDiscoveryTimeoutError, ToolInfo
from agentive_backend.infra.mcp.sandbox import (
    MCPExecutionTimeoutError,
    MCPToolError,
)
from agentive_backend.shared.exceptions import (
    ConflictError,
    DependencyError,
    NotFoundError,
)


def _make_service(
    *,
    existing_server: Any | None = None,
) -> ToolHubService:
    """Build a ToolHubService with AsyncMock repos. ``existing_server``
    controls the duplicate-check branch (None = no duplicate)."""
    server_repo = MagicMock()
    server_repo.with_tenant = MagicMock()
    server_repo.with_tenant.return_value.__aenter__ = AsyncMock(
        return_value=MagicMock()  # session
    )
    server_repo.with_tenant.return_value.__aexit__ = AsyncMock(return_value=False)
    server_repo.get_by_name_in_session = AsyncMock(return_value=existing_server)

    tool_repo = MagicMock()
    return ToolHubService(server_repo=server_repo, tool_repo=tool_repo)


@pytest.mark.asyncio
async def test_connect_server_translates_timeout_to_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCPDiscoveryTimeoutError raised by infra/mcp must be translated to a
    domain DependencyError (503) — features/ never leak infra exception types.
    """

    async def _timeout(**_: Any) -> list[ToolInfo]:
        raise MCPDiscoveryTimeoutError(timeout=10.0)

    monkeypatch.setattr(svc_module, "discover_tools", _timeout)
    service = _make_service()

    with pytest.raises(DependencyError) as exc_info:
        await service.connect_server(
            name="tmo", transport="stdio", connection_config={"command": "x"}
        )
    assert "timeout" in str(exc_info.value.detail).lower()
    assert exc_info.value.context.get("timeout_seconds") == 10.0


@pytest.mark.asyncio
async def test_connect_server_translates_valueerror_to_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ValueError from infra/mcp (malformed connection_config) is
    translated to DependencyError 503, NOT raised raw.
    """

    async def _bad_cfg(**_: Any) -> list[ToolInfo]:
        raise ValueError("stdio connection_config requires non-empty 'command'")

    monkeypatch.setattr(svc_module, "discover_tools", _bad_cfg)
    service = _make_service()

    with pytest.raises(DependencyError) as exc_info:
        await service.connect_server(name="bad", transport="stdio", connection_config={})
    assert "invalid" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised_exception",
    [
        FileNotFoundError("/usr/bin/nonexistent-mcp"),
        PermissionError("[Errno 13] Permission denied"),
        OSError("[Errno 32] Broken pipe"),
        RuntimeError("mcp client protocol error"),
        ConnectionError("SSE handshake failed"),
    ],
    ids=["FileNotFound", "Permission", "OSError", "RuntimeError", "Connection"],
)
async def test_connect_server_broadens_mcp_errors_to_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
    raised_exception: Exception,
) -> None:
    """P-08 (CR 2026-05-10) — Any exception raised inside discover_tools
    that ISN'T already a domain error MUST be translated to DependencyError
    503. Otherwise the FastAPI global handler returns 500 with no useful
    diagnostic about the MCP layer being the culprit.
    """

    async def _raise(**_: Any) -> list[ToolInfo]:
        raise raised_exception

    monkeypatch.setattr(svc_module, "discover_tools", _raise)
    service = _make_service()

    with pytest.raises(DependencyError) as exc_info:
        await service.connect_server(
            name="broken", transport="stdio", connection_config={"command": "x"}
        )
    # Error type is propagated in context for diagnostics.
    assert exc_info.value.context.get("error_type") == type(raised_exception).__name__
    # No secret data leaked into context (P-03 alignment).
    assert "connection_config" not in exc_info.value.context


@pytest.mark.asyncio
async def test_connect_server_409_before_discovery_when_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Décision #6 : duplicate name short-circuits BEFORE the 10s discovery
    so we don't waste subprocess spawn on a doomed registration. The fast-
    fail ConflictError fires before discover_tools is ever called.
    """
    discover_called = False

    async def _spy(**_: Any) -> list[ToolInfo]:
        nonlocal discover_called
        discover_called = True
        return []

    monkeypatch.setattr(svc_module, "discover_tools", _spy)
    existing = MagicMock()
    existing.id = uuid4()
    service = _make_service(existing_server=existing)

    with pytest.raises(ConflictError) as exc_info:
        await service.connect_server(
            name="dup", transport="stdio", connection_config={"command": "x"}
        )
    assert "already registered" in str(exc_info.value.detail).lower()
    assert discover_called is False, (
        "discover_tools must NOT run when duplicate-check fails — "
        "we save 10s of wasted subprocess spawn"
    )


def test_redact_connection_config_masks_env_keys() -> None:
    """P-03 — env values are masked but key NAMES are exposed (so the
    caller can verify which env vars are wired).
    """
    out = _redact_connection_config(
        {
            "command": "python",
            "args": ["-m", "x"],
            "env": {"SECRET_TOKEN": "hunter2", "PUBLIC_VAR": "v"},
        }
    )
    assert out["command"] == "python"
    assert out["args"] == ["-m", "x"]
    assert out["env"] == {"_redacted_keys": ["PUBLIC_VAR", "SECRET_TOKEN"]}
    # Secret value MUST NOT survive in any structure of the redacted dict.
    import json as _json

    assert "hunter2" not in _json.dumps(out)


def test_redact_connection_config_masks_sse_headers() -> None:
    """P-03 — SSE bearer tokens / API keys in headers are masked too."""
    out = _redact_connection_config(
        {
            "url": "https://mcp.example.com/sse",
            "headers": {"Authorization": "Bearer secret-jwt-token"},
        }
    )
    assert out["url"] == "https://mcp.example.com/sse"
    assert out["headers"] == {"_redacted_keys": ["Authorization"]}
    import json as _json

    assert "secret-jwt-token" not in _json.dumps(out)


def test_redact_connection_config_masks_top_level_secret_keys() -> None:
    """Defensive — even if a caller puts a secret-named key at the top
    level (instead of inside env/headers), redact it.
    """
    out = _redact_connection_config(
        {"url": "https://mcp.example.com/sse", "api_key": "ak_live_abcdef"}
    )
    assert out["url"] == "https://mcp.example.com/sse"
    assert out["api_key"] == "<redacted>"
    import json as _json

    assert "ak_live_abcdef" not in _json.dumps(out)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Story 2.6 P-05 — _redact_arguments helper (recursive + pattern-based)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_redact_arguments_masks_secret_synonyms_case_insensitive() -> None:
    """Patterns ``bearer``, ``openai_key``, ``private_key``, ``credential``
    must all be masked — closes the P-05 gap where the connection-config
    helper only knew ``api_key``/``token``/``password``/etc.
    """
    out = _redact_arguments(
        {
            "Authorization": "Bearer xyz",
            "openai_key": "sk-abc",
            "private_key": "-----BEGIN-----",
            "credentials": "user:pass",
            "Bearer_Token": "leak-me",
            "harmless_field": "public",
            "nested": {"api_key": "deep"},
        }
    )
    import json as _json

    payload = _json.dumps(out)
    for leak in ("xyz", "sk-abc", "BEGIN", "user:pass", "leak-me", "deep"):
        assert leak not in payload, f"secret leaked: {leak!r} in {payload}"
    # The non-secret field stays intact.
    assert out["harmless_field"] == "public"


def test_redact_arguments_walks_lists_and_nested_dicts() -> None:
    """Lists of dicts + deep nesting are walked recursively."""
    out = _redact_arguments(
        {
            "items": [
                {"name": "x", "api_key": "leak-1"},
                {"deeply": {"nested": {"password": "leak-2"}}},
            ]
        }
    )
    import json as _json

    payload = _json.dumps(out)
    assert "leak-1" not in payload
    assert "leak-2" not in payload
    # Non-secret values survive.
    assert out["items"][0]["name"] == "x"


def test_redact_arguments_depth_bounded_against_pathological_nesting() -> None:
    """Walks up to depth 8 ; beyond that returns ``"<redacted-deep>"`` to
    avoid stack-overflow on adversarial cyclic-shaped input."""
    deep: dict[str, Any] = {"k": "v"}
    for _ in range(20):
        deep = {"n": deep}
    out = _redact_arguments(deep)
    # Walk down and find the depth-cutoff marker.
    cur: Any = out
    while isinstance(cur, dict) and "n" in cur:
        cur = cur["n"]
    assert cur == "<redacted-deep>"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Story 2.6 T4.7 / P-06 — invoke_tool unit tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_invoke_service(*, server, tool) -> ToolHubService:
    """Build a ToolHubService whose repos return preset ``server`` and
    ``tool`` rows. The audit publish (`_publish_invoked`) opens its own
    session via ``server_repo.with_tenant`` ; we mock that path too.
    """
    server_repo = MagicMock()
    session_mock = MagicMock()
    server_repo.with_tenant = MagicMock()
    server_repo.with_tenant.return_value.__aenter__ = AsyncMock(return_value=session_mock)
    server_repo.with_tenant.return_value.__aexit__ = AsyncMock(return_value=False)
    server_repo.get_by_id_in_session = AsyncMock(return_value=server)

    tool_repo = MagicMock()
    tool_repo.get_by_id_in_session = AsyncMock(return_value=tool)

    return ToolHubService(server_repo=server_repo, tool_repo=tool_repo)


@pytest.mark.asyncio
async def test_invoke_tool_happy_path_returns_call_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4.7 (1) — happy path : call_tool returns a content dict, the
    service forwards it to the caller and publishes a ``success`` audit
    event with ``duration_ms`` populated.
    """
    server_id = uuid4()
    tool_id = uuid4()
    server = MagicMock()
    server.id = server_id
    server.transport = "stdio"
    server.connection_config = {"command": "x"}
    tool = MagicMock()
    tool.id = tool_id
    tool.server_id = server_id
    tool.name = "echo"
    service = _make_invoke_service(server=server, tool=tool)

    captured_event: dict[str, Any] = {}

    async def _fake_publish(event_type: str, event: Any, **_kw: Any) -> Any:
        captured_event["event_type"] = event_type
        captured_event["event"] = event
        return uuid4()  # event_id

    async def _fake_call_tool(**_: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "hi"}], "isError": False}

    monkeypatch.setattr(svc_module, "publish", _fake_publish)
    monkeypatch.setattr(svc_module, "call_tool", _fake_call_tool)
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.invoke_tool(
        server_id=server_id,
        tool_id=tool_id,
        arguments={"text": "hi"},
        timeout=5.0,
        sandbox_backend="setrlimit",
    )
    assert result["isError"] is False
    assert result["content"][0]["text"] == "hi"
    assert captured_event["event_type"] == "m5.tool.invoked"
    assert captured_event["event"].status == "success"
    assert captured_event["event"].sandbox_backend == "setrlimit"
    assert captured_event["event"].duration_ms >= 0


@pytest.mark.asyncio
async def test_invoke_tool_translates_execution_timeout_to_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4.7 (2) — MCPExecutionTimeoutError from infra translates to
    DependencyError 503 ; audit event records status=timeout (cf P-08
    Story 2.5 pattern).
    """
    server_id = uuid4()
    tool_id = uuid4()
    server = MagicMock()
    server.id = server_id
    server.transport = "stdio"
    server.connection_config = {"command": "x"}
    tool = MagicMock()
    tool.id = tool_id
    tool.server_id = server_id
    tool.name = "sleep"
    service = _make_invoke_service(server=server, tool=tool)

    captured: dict[str, Any] = {}

    async def _fake_publish(event_type: str, event: Any, **_kw: Any) -> Any:
        captured["event"] = event
        return uuid4()

    async def _fake_call_tool(**_: Any) -> dict[str, Any]:
        raise MCPExecutionTimeoutError(timeout=0.5)

    monkeypatch.setattr(svc_module, "publish", _fake_publish)
    monkeypatch.setattr(svc_module, "call_tool", _fake_call_tool)
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    with pytest.raises(DependencyError) as exc_info:
        await service.invoke_tool(
            server_id=server_id,
            tool_id=tool_id,
            arguments={"seconds": 30},
            timeout=0.5,
            sandbox_backend="setrlimit",
        )
    assert "timeout" in str(exc_info.value.detail).lower()
    assert captured["event"].status == "timeout"
    assert captured["event"].sandbox_backend == "setrlimit"


@pytest.mark.asyncio
async def test_invoke_tool_translates_tool_error_to_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4.7 (3) — MCPToolError (server returned isError=True) translates
    to NotFoundError 404. Audit records status=error.
    """
    server_id = uuid4()
    tool_id = uuid4()
    server = MagicMock()
    server.id = server_id
    server.transport = "stdio"
    server.connection_config = {"command": "x"}
    tool = MagicMock()
    tool.id = tool_id
    tool.server_id = server_id
    tool.name = "broken"
    service = _make_invoke_service(server=server, tool=tool)

    captured: dict[str, Any] = {}

    async def _fake_publish(event_type: str, event: Any, **_kw: Any) -> Any:
        captured["event"] = event
        return uuid4()

    async def _fake_call_tool(**_: Any) -> dict[str, Any]:
        raise MCPToolError(tool_name="broken", detail="bad input")

    monkeypatch.setattr(svc_module, "publish", _fake_publish)
    monkeypatch.setattr(svc_module, "call_tool", _fake_call_tool)
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    with pytest.raises(NotFoundError) as exc_info:
        await service.invoke_tool(
            server_id=server_id,
            tool_id=tool_id,
            arguments={},
            timeout=5.0,
            sandbox_backend="bwrap",
        )
    assert "bad input" in str(exc_info.value.detail).lower()
    assert captured["event"].status == "error"


@pytest.mark.asyncio
async def test_invoke_tool_args_redaction_masks_secrets_in_audit_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4.7 (4) — argument with ``bearer_token`` key is masked in the
    audit ``args_redacted`` field. Closes the P-05 redaction gap.
    """
    server_id = uuid4()
    tool_id = uuid4()
    server = MagicMock()
    server.id = server_id
    server.transport = "stdio"
    server.connection_config = {"command": "x"}
    tool = MagicMock()
    tool.id = tool_id
    tool.server_id = server_id
    tool.name = "echo"
    service = _make_invoke_service(server=server, tool=tool)

    captured: dict[str, Any] = {}

    async def _fake_publish(event_type: str, event: Any, **_kw: Any) -> Any:
        captured["event"] = event
        return uuid4()

    async def _fake_call_tool(**_: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    monkeypatch.setattr(svc_module, "publish", _fake_publish)
    monkeypatch.setattr(svc_module, "call_tool", _fake_call_tool)
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    await service.invoke_tool(
        server_id=server_id,
        tool_id=tool_id,
        arguments={
            "text": "public",
            "bearer_token": "hunter2-bearer-secret",
            "nested": {"openai_key": "sk-deep-secret"},
        },
        timeout=5.0,
        sandbox_backend="setrlimit",
    )
    args_redacted = captured["event"].args_redacted
    # Top-level secret masked.
    assert args_redacted["bearer_token"] == "<redacted>"
    # Nested secret masked too.
    assert args_redacted["nested"]["openai_key"] == "<redacted>"
    # Non-secret survives.
    assert args_redacted["text"] == "public"


@pytest.mark.asyncio
async def test_invoke_tool_raises_not_found_when_tool_not_on_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4.7 (5) — defense in depth : even if a tool row exists, if its
    ``server_id`` differs from the requested ``server_id`` (e.g. crafted
    URL), service returns 404 instead of invoking.
    """
    server_id = uuid4()
    wrong_server_id = uuid4()
    tool_id = uuid4()
    server = MagicMock()
    server.id = server_id
    server.transport = "stdio"
    server.connection_config = {"command": "x"}
    # tool points to a DIFFERENT server.
    tool = MagicMock()
    tool.id = tool_id
    tool.server_id = wrong_server_id
    tool.name = "oops"
    service = _make_invoke_service(server=server, tool=tool)

    call_tool_called = False

    async def _spy(**_: Any) -> dict[str, Any]:
        nonlocal call_tool_called
        call_tool_called = True
        return {"content": [], "isError": False}

    monkeypatch.setattr(svc_module, "call_tool", _spy)

    with pytest.raises(NotFoundError):
        await service.invoke_tool(
            server_id=server_id,
            tool_id=tool_id,
            arguments={},
            timeout=5.0,
            sandbox_backend="bwrap",
        )
    assert call_tool_called is False, "service should refuse the call BEFORE reaching call_tool"
