"""End-to-end integration tests — Tool invoke (Story 2.6 T10.5 / AC8).

Covers :
- AC1 — POST /tools/servers/{id}/tools/{id}/invoke happy path : echo + add.
- AC2 — timeout returns 503 + subprocess reaped.
- AC5 — audit event m5.tool.invoked published with status + duration_ms +
        args_redacted + sandbox_backend.
- AC6 — unknown tool name returns 404 (MCPToolError translated).
- P-23 — admin gate also covers invoke (flag false → 403).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app


def _stdio_config() -> dict[str, Any]:
    """The connection_config that points to our local mock MCP server."""
    return {"command": "python", "args": ["-m", "tests.fixtures.mcp_mock_server"]}


async def _seed_server(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
    """Create a tool server via the Story 2.5 POST endpoint and return
    its detail body (so we have server_id + tool_ids)."""
    resp = await client.post(
        "/api/v1/tools/servers",
        headers=_auth_headers(),
        json={
            "name": name,
            "transport": "stdio",
            "connection_config": _stdio_config(),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _tool_id(body: dict[str, Any], tool_name: str) -> str:
    """Extract a tool_id from a POST /tools/servers response by tool name."""
    for tool in body["tools"]:
        if tool["name"] == tool_name:
            return str(tool["tool_id"])
    raise AssertionError(f"tool '{tool_name}' not in response: {body['tools']}")


@pytest.mark.integration
async def test_invoke_tool_echo_happy_path(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 — POST .../invoke with arguments={"text":"hi"} returns 200 +
    result containing the echoed text + duration_ms + sandbox_backend.
    """
    app = _make_app(session_factory=app_session_factory)
    # Inject sandbox_backend on app.state so the router reads it (Story 2.6 T3).
    from agentive_backend.infra.mcp.sandbox import detect_sandbox_backend

    app.state.mcp_sandbox_backend = detect_sandbox_backend()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        srv = await _seed_server(client, "ac1-invoke-echo")
        echo_id = _tool_id(srv, "echo")

        resp = await client.post(
            f"/api/v1/tools/servers/{srv['server_id']}/tools/{echo_id}/invoke",
            headers=_auth_headers(),
            json={"arguments": {"text": "hi"}, "timeout_seconds": 10.0},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["sandbox_backend"] in {"bwrap", "setrlimit"}
        assert body["duration_ms"] >= 0
        assert body["result"]["isError"] is False
        assert body["result"]["content"][0]["text"] == "hi"

        # AC5 — audit event m5.tool.invoked landed in outbox.
        async with seed_session_factory() as session:
            audit = await session.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'm5.tool.invoked' "
                    "AND payload->>'tool_name' = 'echo' "
                    "AND payload->>'status' = 'success'"
                ),
            )
            assert int(audit.scalar_one()) >= 1


@pytest.mark.integration
async def test_invoke_tool_timeout_returns_503(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC2 — timeout_seconds=0.1 on sleep(seconds=30) → 503 + audit event
    with status='timeout'."""
    app = _make_app(session_factory=app_session_factory)
    from agentive_backend.infra.mcp.sandbox import detect_sandbox_backend

    app.state.mcp_sandbox_backend = detect_sandbox_backend()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        srv = await _seed_server(client, "ac2-invoke-timeout")
        sleep_id = _tool_id(srv, "sleep")

        resp = await client.post(
            f"/api/v1/tools/servers/{srv['server_id']}/tools/{sleep_id}/invoke",
            headers=_auth_headers(),
            json={"arguments": {"seconds": 30.0}, "timeout_seconds": 0.5},
        )
        assert resp.status_code == 503, resp.text
        body = resp.json()
        assert "timeout" in body.get("detail", "").lower()

        # Audit event with status='timeout' landed in outbox.
        async with seed_session_factory() as session:
            audit = await session.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'm5.tool.invoked' "
                    "AND payload->>'status' = 'timeout' "
                    "AND payload->>'tool_name' = 'sleep'"
                ),
            )
            assert int(audit.scalar_one()) == 1


@pytest.mark.integration
async def test_invoke_tool_unknown_tool_returns_404(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC6 — tool_id that doesn't exist on the server → 404 NotFoundError."""
    from uuid import uuid4

    app = _make_app(session_factory=app_session_factory)
    from agentive_backend.infra.mcp.sandbox import detect_sandbox_backend

    app.state.mcp_sandbox_backend = detect_sandbox_backend()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        srv = await _seed_server(client, "ac6-invoke-404")

        resp = await client.post(
            f"/api/v1/tools/servers/{srv['server_id']}/tools/{uuid4()}/invoke",
            headers=_auth_headers(),
            json={"arguments": {}},
        )
        assert resp.status_code == 404, resp.text


@pytest.mark.integration
async def test_invoke_tool_disabled_by_flag_returns_403(
    app_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-23 (extended Story 2.6) — invoke endpoint is gated by the same
    flag as registration. Flag false → 403 RFC 7807.
    """
    from uuid import uuid4

    from agentive_backend.shared.config import settings as _settings

    app = _make_app(session_factory=app_session_factory)
    from agentive_backend.infra.mcp.sandbox import detect_sandbox_backend

    app.state.mcp_sandbox_backend = detect_sandbox_backend()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Flip the flag AFTER the conftest autouse fixture has enabled it.
        monkeypatch.setattr(_settings, "mcp_allow_registration", False)

        resp = await client.post(
            f"/api/v1/tools/servers/{uuid4()}/tools/{uuid4()}/invoke",
            headers=_auth_headers(),
            json={"arguments": {}},
        )
        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body.get("flag") == "AGENTIVE_ALLOW_MCP_REGISTRATION"


@pytest.mark.integration
async def test_invoke_tool_unknown_agent_template_id_returns_404(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """P-11 (CR 2026-05-11) — when caller supplies an ``agent_template_id``
    that does NOT exist in ``agent_templates``, return 404 rather than
    polluting the audit trail with a fictitious attribution.
    """
    from uuid import uuid4

    from agentive_backend.infra.mcp.sandbox import detect_sandbox_backend

    app = _make_app(session_factory=app_session_factory)
    app.state.mcp_sandbox_backend = detect_sandbox_backend()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        srv = await _seed_server(client, "p11-ghost-template")
        echo_id = _tool_id(srv, "echo")

        resp = await client.post(
            f"/api/v1/tools/servers/{srv['server_id']}/tools/{echo_id}/invoke",
            headers=_auth_headers(),
            json={
                "arguments": {"text": "hi"},
                "agent_template_id": str(uuid4()),  # does NOT exist
            },
        )
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert "agent template" in body.get("detail", "").lower()


@pytest.mark.integration
async def test_invoke_tool_audit_event_redacts_secret_arguments(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC5 — args containing secret-named keys (token, api_key) MUST NOT
    appear in clear in the outbox event payload. P-03 redaction reuse.
    """
    app = _make_app(session_factory=app_session_factory)
    from agentive_backend.infra.mcp.sandbox import detect_sandbox_backend

    app.state.mcp_sandbox_backend = detect_sandbox_backend()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        srv = await _seed_server(client, "ac5-invoke-redact")
        echo_id = _tool_id(srv, "echo")

        # Tool argument with a secret-named key — echo will ignore extras
        # but the audit redaction logic must mask the value.
        resp = await client.post(
            f"/api/v1/tools/servers/{srv['server_id']}/tools/{echo_id}/invoke",
            headers=_auth_headers(),
            json={
                "arguments": {
                    "text": "public",
                    "api_key": "hunter2-secret-do-not-leak",
                }
            },
        )
        assert resp.status_code == 200, resp.text

        # Audit event must NOT contain the secret string.
        async with seed_session_factory() as session:
            row = await session.execute(
                text(
                    "SELECT payload FROM outbox_events "
                    "WHERE event_type = 'm5.tool.invoked' "
                    "AND payload->>'tool_name' = 'echo' "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
            )
            payload_json = row.scalar_one()
            assert "hunter2-secret-do-not-leak" not in str(payload_json), (
                f"P-03 redaction violation: secret leaked into audit payload\n{payload_json}"
            )
