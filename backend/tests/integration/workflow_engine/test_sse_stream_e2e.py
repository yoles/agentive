"""End-to-end integration test — SSE run-events stream (Story 4.2 T10.6).

``httpx.AsyncClient`` in streaming mode against ``GET
/workflows/runs/{run_id}/events``, connected WHILE a real run (real Postgres
checkpointing, mocked LLM) is still executing — verifies the initial `state`
catch-up event, then live `step_completed`/`completed` events as the run
progresses, mirroring a real client watching a run in the UI.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.llm.router import LLMRouter
from agentive_backend.shared.llm.testing import MockProvider
from agentive_backend.shared.llm.types import ChatMessage, Completion
from agentive_backend.shared.repositories import AgentTemplateRepo

from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app
from .conftest import wire_execution_service

pytestmark = pytest.mark.integration

_STREAM_TIMEOUT_S = 15.0
# Slow enough that the SSE client reliably subscribes before the first node
# finishes — the run must still be `running` when the GET connects.
_NODE_DELAY_S = 0.2


class _SlowProvider:
    """Wraps :class:`MockProvider` with an artificial per-call delay so the
    run stays observably in-flight long enough for the SSE test client to
    subscribe before the first node completes."""

    def __init__(self, inner: MockProvider, delay_s: float) -> None:
        self._inner = inner
        self._delay_s = delay_s

    async def complete(self, messages: Sequence[ChatMessage], **kwargs: Any) -> Completion:
        await asyncio.sleep(self._delay_s)
        return await self._inner.complete(messages, **kwargs)


def _completion(text_: str) -> Completion:
    return Completion(
        text=text_,
        model="mock-model",
        provider="mock",
        input_tokens=10,
        output_tokens=5,
        finish_reason="stop",
        latency_ms=5.0,
        cost_estimate_usd=Decimal("0.0001"),
    )


async def _create_template(session_factory: async_sessionmaker[AsyncSession]) -> Any:
    repo = AgentTemplateRepo(session_factory=session_factory)
    return await repo.create(name=f"tpl-{uuid4()}", archetype="producteur", config={})


async def _read_sse_events(response: httpx.Response, *, stop_on: set[str]) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    event_name = "message"
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            events.append((event_name, line.removeprefix("data:").strip()))
            if event_name in stop_on:
                break
    return events


@pytest.mark.asyncio
async def test_sse_stream_receives_state_then_live_step_and_completed_events(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    tpl_a = await _create_template(app_session_factory)
    tpl_b = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    app.state.llm_router = LLMRouter(
        providers={
            "mock": _SlowProvider(
                MockProvider(
                    "mock",
                    [_completion('{"step": "a"}'), _completion('{"step": "b"}')],
                ),
                _NODE_DELAY_S,
            )
        },
        default_chain=["mock"],
    )
    wire_execution_service(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "sse-linear",
                "nodes": [
                    {"node_id": "a", "agent_template_id": str(tpl_a.id)},
                    {"node_id": "b", "agent_template_id": str(tpl_b.id)},
                ],
                "edges": [{"from_node_id": "a", "to_node_id": "b"}],
            },
        )
        workflow_id = create_resp.json()["workflow_id"]

        run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {}},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["run_id"]

        async def _stream() -> list[tuple[str, str]]:
            async with client.stream(
                "GET",
                f"/api/v1/workflows/runs/{run_id}/events",
                headers=_auth_headers(),
            ) as response:
                assert response.status_code == 200
                return await _read_sse_events(response, stop_on={"completed", "failed"})

        events = await asyncio.wait_for(_stream(), timeout=_STREAM_TIMEOUT_S)

    event_names = [name for name, _data in events]
    assert event_names[0] == "state"
    assert "step_completed" in event_names
    assert event_names.count("step_completed") == 2
    assert event_names[-1] == "completed"


@pytest.mark.asyncio
async def test_sse_stream_unknown_run_returns_404(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    app.state.llm_router = LLMRouter(
        providers={"mock": MockProvider("mock", [])}, default_chain=["mock"]
    )
    wire_execution_service(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/workflows/runs/{uuid4()}/events", headers=_auth_headers())
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_sse_stream_already_terminal_run_yields_single_state_event(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """A run already `completed`/`error` by connect time gets ONE `state`
    event, then the stream closes — no pointless wait (AC1)."""
    tpl = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    app.state.llm_router = LLMRouter(
        providers={"mock": MockProvider("mock", [_completion('{"x": 1}')])},
        default_chain=["mock"],
    )
    wire_execution_service(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "sse-mono",
                "nodes": [{"node_id": "a", "agent_template_id": str(tpl.id)}],
                "edges": [],
            },
        )
        workflow_id = create_resp.json()["workflow_id"]
        run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {}},
        )
        run_id = run_resp.json()["run_id"]

        # Poll (via HTTP-adjacent DB check through repeated GET) until terminal
        # before connecting — no artificial delay in this provider.
        async def _wait_terminal() -> None:
            while True:
                async with client.stream(
                    "GET",
                    f"/api/v1/workflows/runs/{run_id}/events",
                    headers=_auth_headers(),
                ) as response:
                    events = await _read_sse_events(response, stop_on={"state"})
                    if events and events[0][1].find('"status": "running"') == -1:
                        return
                await asyncio.sleep(0.05)

        await asyncio.wait_for(_wait_terminal(), timeout=_STREAM_TIMEOUT_S)

        async with client.stream(
            "GET",
            f"/api/v1/workflows/runs/{run_id}/events",
            headers=_auth_headers(),
        ) as response:
            events = await _read_sse_events(response, stop_on={"state"})

    assert len(events) == 1
    assert events[0][0] == "state"
