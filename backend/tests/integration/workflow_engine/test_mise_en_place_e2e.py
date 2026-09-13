"""End-to-end integration test — Mise en Place automatique (Story 4.5 T8.5).

Real Postgres (testcontainers) + a REAL unreachable MCP stdio server
(a nonexistent binary — fails fast via ``FileNotFoundError``, no need to
wait out a real discovery timeout). Unlike every other e2e test in this
package, these tests wire the REAL :class:`MiseEnPlaceService` (see
``_wire_execution_service_with_real_mise_en_place`` below) rather than
``.conftest.wire_execution_service``'s always-pass stub — that stub exists
precisely so tests of OTHER stories don't have to care about this hook,
which makes it the wrong fixture for testing the hook itself.

Scenarios (T8.5):
1. A workflow with a real, unreachable MCP tool assigned → 503, NO
   ``workflow_runs`` row is created — but a ``mise_en_place_refused`` event
   IS written to ``outbox_events`` (review BS2): with no row to hold the
   report, the outbox is the only place a refused launch leaves a trace.
2. Same workflow with ``force=true`` + ``reason`` → the run starts,
   ``mise_en_place.bypassed=true`` is persisted, and
   ``mise_en_place_bypassed`` lands in ``outbox_events``.
3. A workflow with no tools/namespaces/budget cap (i.e. structurally
   healthy) → the run starts normally, ``mise_en_place.all_passed=true``,
   and no bypass event is published.
4. The LLM-provider check never triggers a real provider call — proven by
   the mock provider's call count matching exactly the run's own node
   executions (never more).
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.features.workflow_engine.dry_run import DryRunService, DryRunSettings
from agentive_backend.features.workflow_engine.mise_en_place import (
    MiseEnPlaceService,
    MiseEnPlaceSettings,
)
from agentive_backend.features.workflow_engine.routing_catalog import load_routing_rules
from agentive_backend.features.workflow_engine.service import WorkflowExecutionService
from agentive_backend.shared.llm.router import LLMRouter
from agentive_backend.shared.llm.testing import MockProvider
from agentive_backend.shared.llm.types import Completion
from agentive_backend.shared.repositories import (
    AgentTemplateRepo,
    AgentTemplateToolRepo,
    NamespaceRepo,
    ToolRepo,
    ToolServerRepo,
    WorkflowRepo,
    WorkflowRunRepo,
)

from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app

pytestmark = pytest.mark.integration

#: A binary that cannot possibly exist on PATH — `stdio_client` fails with a
#: quick `FileNotFoundError` instead of waiting out the ping timeout.
_UNREACHABLE_COMMAND = "this-command-does-not-exist-mise-en-place-4-5"

_POLL_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.1


async def _wait_for_terminal_status(factory: async_sessionmaker[AsyncSession], run_id: str) -> str:
    """Poll ``workflow_runs.status`` until it leaves ``running`` (mirror
    ``test_dry_run_e2e.py``'s ``_poll_run_status``).

    Required here specifically because ``postgres_container``
    (``tests/conftest.py``) is SESSION-scoped — every integration test in
    this process shares one Postgres, and ``test_recovery_e2e.py``'s sweep
    (``stale_threshold_s=0.0``) claims EVERY row still ``running`` at the
    time it runs, from ANY test. A run left ``running`` when this test
    function returns is exactly the kind of cross-test pollution the other
    e2e tests in this package already avoid by waiting for a terminal
    status before finishing.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _POLL_TIMEOUT_S
    while loop.time() < deadline:
        async with factory() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM workflow_runs WHERE id = :id"), {"id": run_id}
                )
            ).scalar_one()
        if status in {"completed", "error"}:
            return str(status)
        await asyncio.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"run {run_id} did not reach a terminal status within {_POLL_TIMEOUT_S}s")


def _completion(text_: str) -> Completion:
    return Completion(
        text=text_,
        model="claude-haiku-4-5",
        provider="mock",
        input_tokens=10,
        output_tokens=5,
        finish_reason="stop",
        latency_ms=5.0,
        cost_estimate_usd=Decimal("0.0001"),
    )


async def _create_template(
    session_factory: async_sessionmaker[AsyncSession], *, config: dict[str, Any] | None = None
) -> Any:
    repo = AgentTemplateRepo(session_factory=session_factory)
    return await repo.create(
        name=f"mep-tpl-{uuid4()}",
        archetype="producteur",
        config=config if config is not None else {"llm_model": "claude-haiku-4-5"},
    )


async def _assign_unreachable_tool(
    session_factory: async_sessionmaker[AsyncSession], *, template_id: UUID
) -> None:
    """Register a real ``ToolServer``/``Tool`` pointing at a nonexistent
    stdio command and assign it to ``template_id`` — the
    `mcp_tools_reachable` check must fail against this for real."""
    server_repo = ToolServerRepo(session_factory=session_factory)
    tool_repo = ToolRepo(session_factory=session_factory)
    template_tool_repo = AgentTemplateToolRepo(session_factory=session_factory)

    async with server_repo.with_tenant(None) as session:
        server = await server_repo.create_in_session(
            session,
            name=f"ghost-server-{uuid4()}",
            transport="stdio",
            connection_config={"command": _UNREACHABLE_COMMAND, "args": []},
        )
        tool = await tool_repo.create_in_session(session, server_id=server.id, name="ghost-tool")

    async with template_tool_repo.with_tenant(None) as session:
        await template_tool_repo.replace_in_session(
            session, template_id=template_id, new_tool_ids=[tool.id]
        )


def _wire_execution_service_with_real_mise_en_place(
    app: Any,
    *,
    tool_ping_timeout_s: float = 2.0,
    budget_cap_usd: Decimal | None = None,
) -> None:
    """Mirror ``app.lifespan``'s Story 4.5 wiring (T7.1) on a hand-built
    test app — the REAL :class:`MiseEnPlaceService`, not
    ``.conftest.wire_execution_service``'s always-pass stub.

    Call AFTER ``session_factory``/``workflow_checkpointer``/``llm_router``
    are on ``app.state`` (same precondition as ``.conftest.wire_execution_service``).
    """
    session_factory = app.state.session_factory
    app.state.routing_rules = load_routing_rules()

    dry_run_service = DryRunService(
        workflow_repo=WorkflowRepo(session_factory=session_factory),
        workflow_run_repo=WorkflowRunRepo(session_factory=session_factory),
        template_repo=AgentTemplateRepo(session_factory=session_factory),
        settings=DryRunSettings(
            history_limit=20,
            fallback_input_tokens=500,
            fallback_output_tokens=500,
            budget_cap_usd=budget_cap_usd,
        ),
    )
    mise_en_place_service = MiseEnPlaceService(
        template_tool_repo=AgentTemplateToolRepo(session_factory=session_factory),
        tool_server_repo=ToolServerRepo(session_factory=session_factory),
        namespace_repo=NamespaceRepo(session_factory=session_factory),
        dry_run_service=dry_run_service,
        settings=MiseEnPlaceSettings(
            tool_ping_timeout_s=tool_ping_timeout_s,
            check_timeout_s=15.0,
            budget_cap_usd=budget_cap_usd,
            # Fixed True — this test suite is about `mcp_tools_reachable`
            # and the bypass/no-op paths, not about provider-key presence
            # (covered by `test_mise_en_place_service.py`'s unit tests).
            anthropic_api_key_present=True,
            openai_api_key_present=True,
        ),
    )
    app.state.workflow_execution_service = WorkflowExecutionService(
        workflow_repo=WorkflowRepo(session_factory=session_factory),
        workflow_run_repo=WorkflowRunRepo(session_factory=session_factory),
        template_repo=AgentTemplateRepo(session_factory=session_factory),
        llm_router=app.state.llm_router,
        checkpointer=app.state.workflow_checkpointer,
        routing_rules=app.state.routing_rules,
        mise_en_place_service=mise_en_place_service,
    )


async def _create_workflow(client: httpx.AsyncClient, *, template_id: UUID, name: str) -> str:
    resp = await client.post(
        "/api/v1/workflows",
        headers=_auth_headers(),
        json={
            "name": name,
            "nodes": [{"node_id": "a", "agent_template_id": str(template_id)}],
            "edges": [],
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["workflow_id"])


@pytest.mark.asyncio
async def test_start_run_blocked_by_unreachable_mcp_tool(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    tpl = await _create_template(app_session_factory)
    await _assign_unreachable_tool(app_session_factory, template_id=tpl.id)

    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    app.state.llm_router = LLMRouter(
        providers={"mock": MockProvider("mock", [])}, default_chain=["mock"]
    )
    _wire_execution_service_with_real_mise_en_place(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _create_workflow(
            client, template_id=tpl.id, name="e2e-mise-en-place-blocked"
        )
        run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {}},
        )

    assert run_resp.status_code == 503, run_resp.text
    body = run_resp.json()
    assert body["type"] == "/errors/dependency"
    assert body["failed_checks"] == ["mcp_tools_reachable"]

    async with seed_session_factory() as session:
        count = await session.execute(
            text("SELECT COUNT(*) FROM workflow_runs WHERE workflow_id = :wid"),
            {"wid": workflow_id},
        )
        assert int(count.scalar_one()) == 0

        # ...but the refusal IS traced in the outbox (review BS2): with no
        # row to persist the report on, the trace would otherwise vanish the
        # moment the 503 was returned. Keyed on `workflow_id` — `run_id`
        # stays `None` on THIS path (Story 4.12 AC4 populates it only from
        # `resume`, where a run genuinely exists), because there is no run.
        events = (
            (
                await session.execute(
                    text(
                        "SELECT event_type, payload FROM outbox_events "
                        "WHERE payload->>'workflow_id' = :wid"
                    ),
                    {"wid": workflow_id},
                )
            )
            .mappings()
            .all()
        )
        refusals = [e for e in events if e["event_type"].endswith("mise_en_place_refused")]
        assert len(refusals) == 1
        payload = refusals[0]["payload"]
        assert payload["failed_checks"] == ["mcp_tools_reachable"]
        assert payload["retryable"] is True
        assert payload["mise_en_place"]["all_passed"] is False
        assert payload["run_id"] is None
        # And no `started` event leaked out for a launch that never happened.
        assert not any(e["event_type"].endswith("run.started") for e in events)


@pytest.mark.asyncio
async def test_start_run_bypasses_unreachable_tool_with_force(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    tpl = await _create_template(app_session_factory)
    await _assign_unreachable_tool(app_session_factory, template_id=tpl.id)

    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    provider = MockProvider("mock", [_completion(json.dumps({"status": "done"}))])
    app.state.llm_router = LLMRouter(providers={"mock": provider}, default_chain=["mock"])
    _wire_execution_service_with_real_mise_en_place(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _create_workflow(
            client, template_id=tpl.id, name="e2e-mise-en-place-bypassed"
        )
        run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {}, "force": True, "reason": "incident P1, deadline serrée"},
        )

    assert run_resp.status_code == 201, run_resp.text
    body = run_resp.json()
    assert body["mise_en_place"]["bypassed"] is True
    assert body["mise_en_place"]["bypass_reason"] == "incident P1, deadline serrée"
    run_id = body["run_id"]
    assert await _wait_for_terminal_status(seed_session_factory, run_id) == "completed"

    async with seed_session_factory() as session:
        row = (
            (
                await session.execute(
                    text("SELECT mise_en_place FROM workflow_runs WHERE id = :id"),
                    {"id": run_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["mise_en_place"]["bypassed"] is True
        assert row["mise_en_place"]["bypass_reason"] == "incident P1, deadline serrée"

        events = (
            (
                await session.execute(
                    text("SELECT event_type FROM outbox_events WHERE payload->>'run_id' = :rid"),
                    {"rid": run_id},
                )
            )
            .scalars()
            .all()
        )
        assert "workflow_engine.workflow_run.mise_en_place_bypassed" in events
        assert "workflow_engine.workflow_run.started" in events


@pytest.mark.asyncio
async def test_start_run_healthy_workflow_passes_every_check(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    """No tools, no namespace, no budget cap configured — every check
    passes, the run starts normally, and NO bypass event is published even
    though nothing was forced."""
    tpl = await _create_template(app_session_factory)

    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    provider = MockProvider("mock", [_completion(json.dumps({"status": "done"}))])
    app.state.llm_router = LLMRouter(providers={"mock": provider}, default_chain=["mock"])
    _wire_execution_service_with_real_mise_en_place(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _create_workflow(
            client, template_id=tpl.id, name="e2e-mise-en-place-healthy"
        )
        run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {}},
        )

    assert run_resp.status_code == 201, run_resp.text
    body = run_resp.json()
    assert body["mise_en_place"]["all_passed"] is True
    assert body["mise_en_place"]["bypassed"] is False
    assert len(body["mise_en_place"]["checks"]) == 4
    assert all(check["passed"] for check in body["mise_en_place"]["checks"])
    run_id = body["run_id"]
    assert await _wait_for_terminal_status(seed_session_factory, run_id) == "completed"

    async with seed_session_factory() as session:
        events = (
            (
                await session.execute(
                    text("SELECT event_type FROM outbox_events WHERE payload->>'run_id' = :rid"),
                    {"rid": run_id},
                )
            )
            .scalars()
            .all()
        )
        assert "workflow_engine.workflow_run.mise_en_place_bypassed" not in events

    # AC1's `llm_providers_configured`/`budget_available` checks never call
    # a real provider (structural guarantee, `DryRunService` has no
    # `llm_router` at all — cf its class docstring). Only the run's own
    # node execution below is allowed to have called the mock provider.
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_mise_en_place_check_never_triggers_a_real_provider_call(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    """Dedicated assertion for T8.5's 4th bullet — the Mise en Place hook
    makes ZERO real provider calls.

    Deliberately exercised on the BLOCKING path (review P9). The previous
    version started a real run and asserted ``len(provider.calls) <= 1`` on
    a value that can only be 0 or 1, racing the background task: it passed
    whatever the hook did, and could not distinguish a call made by the hook
    from one made by the run. Here a failing check means no run is ever
    created and no node ever executes, so ``calls == []`` is attributable to
    the hook alone and cannot be reached by a lucky race.

    (T8.5 named an autouse ``_no_external_http`` fixture for this; no such
    fixture exists in ``tests/conftest.py`` — the guarantee is behavioural,
    asserted here, not structural.)
    """
    tpl = await _create_template(app_session_factory)
    await _assign_unreachable_tool(app_session_factory, template_id=tpl.id)

    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    provider = MockProvider("mock", [_completion(json.dumps({"status": "done"}))])
    app.state.llm_router = LLMRouter(providers={"mock": provider}, default_chain=["mock"])
    _wire_execution_service_with_real_mise_en_place(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _create_workflow(
            client, template_id=tpl.id, name="e2e-mise-en-place-no-provider-call"
        )
        run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {}},
        )

    assert run_resp.status_code == 503, run_resp.text
    assert provider.calls == []
    # No drain needed here, unlike the scenarios that actually start a run:
    # the launch was refused, so no `workflow_runs` row exists to leave
    # `running` and pollute `test_recovery_e2e.py`'s session-scoped sweep.
