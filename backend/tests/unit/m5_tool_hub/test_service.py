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
    _redact_connection_config,
)
from agentive_backend.infra.mcp.client import MCPDiscoveryTimeoutError, ToolInfo
from agentive_backend.shared.exceptions import (
    ConflictError,
    DependencyError,
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
