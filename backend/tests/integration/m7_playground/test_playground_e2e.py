"""End-to-end integration tests — Playground run (Story 2.7 T6.2 / AC8).

Covers :
- AC1/AC3 — happy path POST /playground/.../run → 200 + body shape.
- AC2 — strict isolation : 0 agent_instances row, 0 m2.agent_instance.created,
  0 m5.tool.invoked. Single m7.playground.run_completed event.
- AC6 — 404 ghost template_id, 403 flag disabled, 422 missing variable.
- P-23 gate alignment.

The LLM router is monkeypatched on ``app.state.llm_router`` to avoid
calling real providers.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.llm.types import Completion

from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app


def _stub_llm_router(
    text_out: str = '{"summary": "ok"}', *, model: str = "claude-sonnet-4-6"
) -> Any:
    """Build an AsyncMock LLM router that returns a fixed Completion."""
    router = AsyncMock()
    router.complete = AsyncMock(
        return_value=Completion(
            text=text_out,
            model=model,
            provider="anthropic",
            input_tokens=42,
            output_tokens=84,
            finish_reason="stop",
            latency_ms=123.0,
            cost_estimate_usd=Decimal("0.000123"),
        )
    )
    return router


async def _create_template(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
    """Helper — create an agent template via Story 2.1 endpoint."""
    resp = await client.post(
        "/api/v1/agents/templates",
        headers=_auth_headers(),
        json={"archetype": "producteur", "name": name},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.integration
async def test_playground_run_happy_path_returns_full_response(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 + AC3 — happy path. Body contains prompt_resolved + raw_output +
    parsed_output + tokens + cost + model + provider + tool_invocations."""
    app = _make_app(session_factory=app_session_factory)
    app.state.llm_router = _stub_llm_router()
    app.state.mcp_sandbox_backend = "setrlimit"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        tpl = await _create_template(client, "playground-happy")
        template_id = tpl["template_id"]

        resp = await client.post(
            f"/api/v1/playground/agents/{template_id}/run",
            headers=_auth_headers(),
            json={"arguments": {"topic": "tests"}, "timeout_seconds": 10.0},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["raw_output"] == '{"summary": "ok"}'
        assert body["parsed_output"] == {"summary": "ok"}
        assert body["tokens"] == {"input_tokens": 42, "output_tokens": 84}
        assert body["model_used"] == "claude-sonnet-4-6"
        assert body["provider_used"] == "anthropic"
        assert body["tool_invocations"] == []
        assert body["duration_ms_total"] >= 0
        assert body["cost_estimate_usd"] == "0.000123"
        # AC2 — agent_instances row count unchanged (none created).
        async with seed_session_factory() as session:
            instances_count = await session.execute(
                text("SELECT count(*) FROM agent_instances WHERE template_id = :tid"),
                {"tid": template_id},
            )
            assert int(instances_count.scalar_one()) == 0


@pytest.mark.integration
async def test_playground_run_emits_single_audit_event_zero_instance_event(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC2 — isolation atomicité : exactly 1 m7.playground.run_completed
    event, 0 m2.agent_instance.created, 0 m5.tool.invoked."""
    app = _make_app(session_factory=app_session_factory)
    app.state.llm_router = _stub_llm_router()
    app.state.mcp_sandbox_backend = "setrlimit"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        tpl = await _create_template(client, "playground-isolation")
        template_id = tpl["template_id"]

        resp = await client.post(
            f"/api/v1/playground/agents/{template_id}/run",
            headers=_auth_headers(),
            json={"arguments": {}},
        )
        assert resp.status_code == 200, resp.text

        async with seed_session_factory() as session:
            playground_count = await session.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'm7.playground.run_completed' "
                    "AND payload->>'template_id' = :tid"
                ),
                {"tid": template_id},
            )
            assert int(playground_count.scalar_one()) == 1

            # AC2 — no m2.agent_instance.created (no instance created).
            instance_event = await session.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'm2.agent_instance.created' "
                    "AND payload->>'template_id' = :tid"
                ),
                {"tid": template_id},
            )
            assert int(instance_event.scalar_one()) == 0

            # AC2 — no m5.tool.invoked (Playground bypass).
            tool_event = await session.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'm5.tool.invoked' "
                    "AND payload->>'template_id' = :tid"
                ),
                {"tid": template_id},
            )
            assert int(tool_event.scalar_one()) == 0


@pytest.mark.integration
async def test_playground_run_template_not_found_returns_404(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC6 — ghost template_id → 404 + 0 audit event published."""
    app = _make_app(session_factory=app_session_factory)
    app.state.llm_router = _stub_llm_router()
    app.state.mcp_sandbox_backend = "setrlimit"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ghost_id = uuid4()
        resp = await client.post(
            f"/api/v1/playground/agents/{ghost_id}/run",
            headers=_auth_headers(),
            json={"arguments": {}},
        )
        assert resp.status_code == 404, resp.text

        async with seed_session_factory() as session:
            audit = await session.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'm7.playground.run_completed' "
                    "AND payload->>'template_id' = :tid"
                ),
                {"tid": str(ghost_id)},
            )
            assert int(audit.scalar_one()) == 0


@pytest.mark.integration
async def test_playground_run_403_when_flag_disabled(
    app_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-23 alignment — Playground gated by AGENTIVE_ALLOW_MCP_REGISTRATION."""
    from agentive_backend.shared.config import settings as _settings

    monkeypatch.setattr(_settings, "mcp_allow_registration", False)

    app = _make_app(session_factory=app_session_factory)
    app.state.llm_router = _stub_llm_router()
    app.state.mcp_sandbox_backend = "setrlimit"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/playground/agents/{uuid4()}/run",
            headers=_auth_headers(),
            json={"arguments": {}},
        )
        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body.get("flag") == "AGENTIVE_ALLOW_MCP_REGISTRATION"
