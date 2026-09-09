"""End-to-end integration tests — Playground run (Story 2.7 T6.2 / AC8).

Covers :
- AC1/AC3 — happy path POST /playground/.../run → 200 + body shape.
- AC2 — strict isolation : 0 agent_instances row, 0 agent_registry.agent_instance.created,
  0 tool_hub.tool.invoked. Single playground.run.completed event.
- AC6 — 404 ghost template_id, 403 flag disabled, 422 missing variable
  (P-18 fix-batch 2026-08-31 — this docstring used to claim the 422 case
  was covered here when only 404/403 actually were).
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

from agentive_backend.features.memory_manager.push_memory import (
    MemoryManagerPushMemoryProvider,
)
from agentive_backend.features.memory_manager.service import MemoryManagerService
from agentive_backend.shared.llm.types import Completion
from agentive_backend.shared.repositories import ChunkEmbeddingRepo, MemoryChunkRepo, NamespaceRepo

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


async def _create_namespace(session_factory: async_sessionmaker[AsyncSession], *, name: str) -> Any:
    """Namespace created directly via the repo — mirror
    ``tests/integration/memory_manager/test_api.py``'s own helper : namespace
    CRUD is a Story 3.2 concern, orthogonal to what this Push Memory e2e
    test (Story 3.5) actually exercises."""
    repo = NamespaceRepo(session_factory=session_factory)
    return await repo.create(name=name, ns_type="metier")


def _wire_push_memory_provider(
    app: Any, *, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Mirror ``app/lifespan.py`` T11.1's wiring exactly, against the same
    ``EmbeddingRouter`` the app already uses (set by ``make_e2e_app``) so a
    chunk and a query embed identically to the SAME test data this test
    just wrote."""
    app.state.push_memory_provider = MemoryManagerPushMemoryProvider(
        memory_manager_service=MemoryManagerService(
            memory_chunk_repo=MemoryChunkRepo(session_factory=session_factory),
            chunk_embedding_repo=ChunkEmbeddingRepo(session_factory=session_factory),
            namespace_repo=NamespaceRepo(session_factory=session_factory),
            embedding_router=app.state.embedding_router,
        )
    )


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
    """AC2 — isolation atomicité : exactly 1 playground.run.completed
    event, 0 agent_registry.agent_instance.created, 0 tool_hub.tool.invoked."""
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
            playground_status = await session.execute(
                text(
                    "SELECT payload->>'status' FROM outbox_events "
                    "WHERE event_type = 'playground.run.completed' "
                    "AND payload->>'template_id' = :tid"
                ),
                {"tid": template_id},
            )
            rows = playground_status.scalars().all()
            # P-06 (fix-batch 2026-08-31) — the AC7 code-review demanded proof
            # of a 200-success path with an audited `status=success` (the
            # manual smoke capture in the story's Completion Notes only ever
            # ran without a real ANTHROPIC_API_KEY → 503 llm_error every
            # time). This is the programmatic equivalent : a stubbed LLM
            # router that returns a normal completion, asserting the audit
            # payload's status field end-to-end through the real DB.
            assert rows == ["success"]

            # AC2 — no agent_registry.agent_instance.created (no instance created).
            instance_event = await session.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'agent_registry.agent_instance.created' "
                    "AND payload->>'template_id' = :tid"
                ),
                {"tid": template_id},
            )
            assert int(instance_event.scalar_one()) == 0

            # AC2 — no tool_hub.tool.invoked (Playground bypass).
            tool_event = await session.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'tool_hub.tool.invoked' "
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
                    "WHERE event_type = 'playground.run.completed' "
                    "AND payload->>'template_id' = :tid"
                ),
                {"tid": str(ghost_id)},
            )
            assert int(audit.scalar_one()) == 0


@pytest.mark.integration
async def test_playground_run_422_when_variable_missing_from_arguments(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """P-18 (fix-batch 2026-08-31) — AC6 : a template whose system_prompt
    references a variable absent from ``arguments`` → 422 RFC 7807, 0 audit
    event. Previously only unit-tested (test_service.py); this closes the
    gap at the HTTP + DB layer the docstring already (wrongly) claimed."""
    app = _make_app(session_factory=app_session_factory)
    app.state.llm_router = _stub_llm_router()
    app.state.mcp_sandbox_backend = "setrlimit"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        tpl = await _create_template(client, "playground-422-missing-var")
        template_id = tpl["template_id"]
        # Story 2.2 PUT — set a system_prompt referencing {topic}, then omit
        # it from the Playground run's arguments to trigger the 422 path.
        put_resp = await client.put(
            f"/api/v1/agents/templates/{template_id}",
            headers=_auth_headers(),
            json={"system_prompt": "Summarize {topic} for the reader."},
        )
        assert put_resp.status_code == 200, put_resp.text

        resp = await client.post(
            f"/api/v1/playground/agents/{template_id}/run",
            headers=_auth_headers(),
            json={"arguments": {}},
        )
        assert resp.status_code == 422, resp.text

        async with seed_session_factory() as session:
            audit = await session.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'playground.run.completed' "
                    "AND payload->>'template_id' = :tid"
                ),
                {"tid": template_id},
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


@pytest.mark.integration
async def test_playground_run_injects_memorized_chunk_end_to_end(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Story 3.5 AC1/AC3, T12.6 — a real namespace + a real chunk, written
    through the actual ``POST /memory/chunks`` endpoint, over the deterministic
    ``MockEmbedder`` (identical text embeds identically, cf
    ``tests/integration/memory_manager/test_api.py``'s
    ``test_search_happy_path_ranks_exact_match_first``), get proactively
    injected into a Playground run's prompt end-to-end : real Postgres ANN
    search, real threshold filter, real HTTP response shape."""
    remembered = "apples and oranges"
    await _create_namespace(app_session_factory, name="e2e-push-memory")

    app = _make_app(session_factory=app_session_factory)
    app.state.llm_router = _stub_llm_router()
    app.state.mcp_sandbox_backend = "setrlimit"
    _wire_push_memory_provider(app, session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        chunk_resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": remembered, "namespace": "e2e-push-memory"},
        )
        assert chunk_resp.status_code == 201, chunk_resp.text

        tpl = await _create_template(client, "playground-push-memory")
        template_id = tpl["template_id"]
        # system_prompt IS the remembered content, verbatim — under the
        # deterministic MockEmbedder this guarantees a near-1.0 similarity
        # (same text embeds identically), comfortably above the 0.85
        # threshold, with zero flakiness.
        put_resp = await client.put(
            f"/api/v1/agents/templates/{template_id}",
            headers=_auth_headers(),
            json={
                "system_prompt": remembered,
                "push_memory": {"namespace": "e2e-push-memory", "optin": False},
            },
        )
        assert put_resp.status_code == 200, put_resp.text

        resp = await client.post(
            f"/api/v1/playground/agents/{template_id}/run",
            headers=_auth_headers(),
            json={"arguments": {}},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["push_memory"] is not None
        assert body["push_memory"]["chunks_injected"] == 1
        assert body["push_memory"]["tokens_used"] > 0
        assert 'source="e2e-push-memory/' in body["prompt_resolved"]
        assert body["prompt_resolved"].endswith(remembered)


@pytest.mark.integration
async def test_playground_run_without_push_memory_wired_returns_null(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 last point / non-regression — a template configuring
    ``push_memory.namespace`` still returns ``push_memory: null`` when
    ``app.state.push_memory_provider`` isn't wired (this suite's other
    tests never set it) : this closes the same gap the unit-level
    ``test_push_memory_provider_none_is_non_regression`` covers, at the
    HTTP + real-DB layer.

    P15 (revue 3.5) : no namespace is created here on purpose. This test
    exercises the path where the provider is not wired at all, so the lookup
    never happens and a real namespace would be dead setup, suggesting a
    coverage this test does not provide."""
    app = _make_app(session_factory=app_session_factory)
    app.state.llm_router = _stub_llm_router()
    app.state.mcp_sandbox_backend = "setrlimit"
    # Deliberately NOT calling _wire_push_memory_provider here.

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        tpl = await _create_template(client, "playground-push-memory-unwired")
        template_id = tpl["template_id"]
        put_resp = await client.put(
            f"/api/v1/agents/templates/{template_id}",
            headers=_auth_headers(),
            json={
                "system_prompt": "hello",
                "push_memory": {"namespace": "e2e-push-memory-unwired", "optin": False},
            },
        )
        assert put_resp.status_code == 200, put_resp.text

        resp = await client.post(
            f"/api/v1/playground/agents/{template_id}/run",
            headers=_auth_headers(),
            json={"arguments": {}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["push_memory"] is None
