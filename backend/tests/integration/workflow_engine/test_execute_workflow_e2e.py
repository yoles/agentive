"""End-to-end integration test — workflow execution (Story 4.2 T10.4).

Real Postgres, real ``AsyncPostgresSaver`` checkpointing (T1.2 migration),
mocked LLM (``MockProvider`` — no network calls). Exercises the full path:
``POST /workflows/{id}/runs`` → background ``_drive_run`` → per-node
checkpoint sync (AC2) → ``completed`` status + aggregated metrics (AC4) →
outbox events for every lifecycle transition (T6).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.llm.router import LLMRouter
from agentive_backend.shared.llm.testing import MockProvider
from agentive_backend.shared.llm.types import Completion
from agentive_backend.shared.repositories import AgentTemplateRepo

from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app
from .conftest import wire_execution_service

pytestmark = pytest.mark.integration

_POLL_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.1


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


async def _poll_run_status(
    factory: async_sessionmaker[AsyncSession], run_id: str, *, terminal: set[str]
) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + _POLL_TIMEOUT_S
    while asyncio.get_event_loop().time() < deadline:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT status, checkpoint, metrics, last_checkpoint_at "
                    "FROM workflow_runs WHERE id = :id"
                ),
                {"id": run_id},
            )
            row = result.mappings().one()
        if row["status"] in terminal:
            return dict(row)
        await asyncio.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"run {run_id} did not reach a terminal status within {_POLL_TIMEOUT_S}s")


async def _count_outbox(
    factory: async_sessionmaker[AsyncSession], event_type: str, *, run_id: str
) -> int:
    async with factory() as session:
        result = await session.execute(
            text(
                "SELECT COUNT(*) FROM outbox_events "
                "WHERE event_type = :t AND payload->>'run_id' = :r"
            ),
            {"t": event_type, "r": run_id},
        )
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_execute_workflow_e2e_completes_with_checkpoint_and_metrics(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    tpl_a = await _create_template(app_session_factory)
    tpl_b = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    app.state.llm_router = LLMRouter(
        providers={
            "mock": MockProvider(
                "mock",
                [_completion('{"step": "a"}'), _completion('{"step": "b"}')],
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
                "name": "e2e-linear",
                "nodes": [
                    {"node_id": "a", "agent_template_id": str(tpl_a.id)},
                    {"node_id": "b", "agent_template_id": str(tpl_b.id)},
                ],
                "edges": [{"from_node_id": "a", "to_node_id": "b"}],
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        workflow_id = create_resp.json()["workflow_id"]

        started = datetime.now(UTC)
        run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {"question": "hello"}},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_body = run_resp.json()
        assert run_body["status"] == "running"
        run_id = run_body["run_id"]

        # The AC1 property (201 returns BEFORE the run finishes) is asserted
        # deterministically by `test_start_run_returns_before_the_run_finishes`
        # below, which gates the LLM so the run CANNOT have completed. Here we
        # only need the run to reach its terminal state.
        row = await _poll_run_status(seed_session_factory, run_id, terminal={"completed", "error"})

    assert row["status"] == "completed", row
    assert (datetime.now(UTC) - started).total_seconds() < _POLL_TIMEOUT_S

    checkpoint = row["checkpoint"]
    assert checkpoint["last_node_id"] == "b"
    assert checkpoint["node_statuses"] == {"a": "success", "b": "success"}
    assert row["last_checkpoint_at"] is not None

    metrics = row["metrics"]
    assert metrics["total_tokens"]["input"] == 20
    assert metrics["total_tokens"]["output"] == 10
    assert set(metrics["per_node"]) == {"a", "b"}

    for event_type in (
        "workflow_engine.workflow_run.started",
        "workflow_engine.workflow_run.step_completed",
        "workflow_engine.workflow_run.completed",
    ):
        count = await _count_outbox(seed_session_factory, event_type, run_id=run_id)
        assert count >= 1, f"missing {event_type}"

    step_completed_count = await _count_outbox(
        seed_session_factory, "workflow_engine.workflow_run.step_completed", run_id=run_id
    )
    assert step_completed_count == 2


@pytest.mark.asyncio
async def test_execute_workflow_e2e_llm_failure_marks_run_error(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    """AC4 — a node LLM failure terminates the run as `error`, not silently."""
    tpl_a = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    app.state.llm_router = LLMRouter(
        providers={"mock": MockProvider("mock", [RuntimeError("provider unavailable")])},
        default_chain=["mock"],
    )
    wire_execution_service(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "e2e-failing",
                "nodes": [{"node_id": "a", "agent_template_id": str(tpl_a.id)}],
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

        row = await _poll_run_status(seed_session_factory, run_id, terminal={"completed", "error"})

    assert row["status"] == "error"
    assert "providers failed" in row["checkpoint"]["last_error"]

    count = await _count_outbox(
        seed_session_factory, "workflow_engine.workflow_run.failed", run_id=run_id
    )
    assert count == 1


@pytest.mark.asyncio
async def test_start_run_unknown_workflow_returns_404(
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
        resp = await client.post(
            f"/api/v1/workflows/{uuid4()}/runs",
            headers=_auth_headers(),
            json={"input": {}},
        )
    assert resp.status_code == 404
    body = resp.json()
    assert body["type"] == "/errors/not-found"


@pytest.mark.asyncio
async def test_start_run_inactive_workflow_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    tpl = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    app.state.llm_router = LLMRouter(
        providers={"mock": MockProvider("mock", [])}, default_chain=["mock"]
    )
    wire_execution_service(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "e2e-inactive",
                "nodes": [{"node_id": "a", "agent_template_id": str(tpl.id)}],
                "edges": [],
            },
        )
        workflow_id = create_resp.json()["workflow_id"]

        async with seed_session_factory() as session:
            await session.execute(
                text("UPDATE workflows SET status = 'draft' WHERE id = :id"),
                {"id": workflow_id},
            )
            await session.commit()

        resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {}},
        )
    assert resp.status_code == 422
    assert resp.json()["type"] == "/errors/validation"


class _GatedProvider:
    """LLM provider that blocks until the test releases it.

    Makes AC1's "la requête HTTP NE bloque PAS jusqu'à la fin du run"
    deterministically observable. The previous assertion —
    ``assert immediate_status in {"running", "completed"}`` — accepted every
    possible status a non-terminal-or-terminal run can have, so it held just
    as well for a `start_run` that blocked until completion. With the node
    gated, the run CANNOT have finished when the POST returns, and
    ``status == "running"`` becomes a real claim.
    """

    provider_name = "gated"

    def __init__(self, gate: asyncio.Event, completion: Completion) -> None:
        self._gate = gate
        self._completion = completion
        self.call_count = 0

    async def complete(self, messages: Any, **_kwargs: Any) -> Completion:
        self.call_count += 1
        await self._gate.wait()
        return self._completion


@pytest.mark.asyncio
async def test_start_run_returns_before_the_run_finishes(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    """AC1 — the 201 must come back while the run is still executing."""
    tpl = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    gate = asyncio.Event()
    provider = _GatedProvider(gate, _completion('{"step": "a"}'))
    app.state.llm_router = LLMRouter(providers={"gated": provider}, default_chain=["gated"])
    wire_execution_service(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "e2e-gated",
                "nodes": [{"node_id": "a", "agent_template_id": str(tpl.id)}],
                "edges": [],
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        run_resp = await client.post(
            f"/api/v1/workflows/{create_resp.json()['workflow_id']}/runs",
            headers=_auth_headers(),
            json={"input": {"question": "hello"}},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["run_id"]

        # Let the background task reach the (blocked) LLM call.
        for _ in range(50):
            if provider.call_count:
                break
            await asyncio.sleep(0.02)
        assert provider.call_count == 1, "the run never reached its LLM call"

        # The node is still blocked, so the run CANNOT be terminal.
        async with seed_session_factory() as session:
            result = await session.execute(
                text("SELECT status FROM workflow_runs WHERE id = :id"), {"id": run_id}
            )
            assert result.scalar_one() == "running"

        # The caller's input was stamped on the row at creation so a crash
        # before LangGraph's first checkpoint is still recoverable (AC3).
        async with seed_session_factory() as session:
            result = await session.execute(
                text("SELECT checkpoint FROM workflow_runs WHERE id = :id"), {"id": run_id}
            )
            assert result.scalar_one()["task_input"] == {"question": "hello"}

        gate.set()
        row = await _poll_run_status(seed_session_factory, run_id, terminal={"completed", "error"})

    assert row["status"] == "completed", row
