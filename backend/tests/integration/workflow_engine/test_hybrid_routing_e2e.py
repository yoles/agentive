"""End-to-end integration test — hybrid routing (Story 4.3 T11.9).

Real Postgres, real ``AsyncPostgresSaver`` checkpointing, real production
routing-rules catalog (``routing_catalog.load_routing_rules()``), mocked LLM
(``MockProvider`` — no network calls, per ``shared/llm/testing.py`` and the
``_no_external_http`` autouse fixture).

Three scenarios (T11.9):

1. A node whose own output satisfies a declarative rule (``status: "done"``)
   never escalates — the ``MockProvider`` receives exactly ONE call total
   (the node's own completion), never a second one for an escalation.
2. A node whose output matches no rule escalates; the mocked LLM response
   picks a declared candidate, the run continues down that branch, and
   ``metrics["routing"]["llm_escalated"] == 1`` /
   ``checkpoint["routing_decisions"]`` are populated.
3. ``GET /workflows/{workflow_id}/routing-stats`` reports the expected ratio
   for a workflow with runs, and ``deterministic_pct: null`` for one with
   none (AC3's explicit "no decision point ever reached" state).
"""

from __future__ import annotations

import asyncio
import json
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
    return await repo.create(name=f"tpl-{uuid4()}", archetype="producteur", config=config or {})


# `output.status` is referenced by every edge condition below — Story 4.1
# AC3 requires the EMITTING node's `output_contract.core` to expose any
# variable a branching condition names, or `POST /workflows` refuses the
# DAG with 422 at creation time (before this story's routing layer ever
# runs).
_EMITS_STATUS_CONFIG: dict[str, Any] = {"output_contract": {"core": {"status": {"type": "string"}}}}


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
async def test_declarative_rule_resolves_without_escalation(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    """Scenario 1 — `output.status == 'done'` satisfies the shipped
    `terminal-output-status` rule; the run terminates WITHOUT ever calling
    the LLM a second time for an escalation."""
    tpl_a = await _create_template(app_session_factory, config=_EMITS_STATUS_CONFIG)
    tpl_b = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    provider = MockProvider("mock", [_completion(json.dumps({"status": "done"}))])
    app.state.llm_router = LLMRouter(providers={"mock": provider}, default_chain=["mock"])
    wire_execution_service(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "e2e-rule-decides",
                "nodes": [
                    {"node_id": "a", "agent_template_id": str(tpl_a.id)},
                    {"node_id": "b", "agent_template_id": str(tpl_b.id)},
                ],
                "edges": [
                    {
                        "from_node_id": "a",
                        "to_node_id": "b",
                        "condition": "output.status == 'never'",
                    }
                ],
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        workflow_id = create_resp.json()["workflow_id"]

        run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {}},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["run_id"]

        row = await _poll_run_status(seed_session_factory, run_id, terminal={"completed", "error"})

    assert row["status"] == "completed", row
    # Node `b` never ran — the rule terminated the branch. `_mark_completed`
    # (Story 4.2 AC4) reports it as "skipped", not silently absent.
    assert row["checkpoint"]["node_statuses"] == {"a": "success", "b": "skipped"}
    routing = row["metrics"]["routing"]
    assert routing["deterministic"] == 1
    assert routing["llm_escalated"] == 0
    # A deterministic decision spends NOTHING — that is the whole point of the
    # feature, and IG1's cost block is what finally makes it measurable.
    assert routing["cost_usd"] is None
    assert routing["tokens"] == {"input": 0, "output": 0}
    decision = row["checkpoint"]["routing_decisions"]["a"]
    assert decision["mode"] == "deterministic"
    assert decision["source"] == "rules"
    assert decision["rule_id"] == "terminal-output-status"

    # Exactly ONE provider call total — the node's own completion. A second
    # call would mean an (unwanted) escalation happened.
    assert len(provider.calls) == 1

    escalated_count = await _count_outbox(
        seed_session_factory, "workflow_engine.workflow_run.routing_escalated", run_id=run_id
    )
    assert escalated_count == 0


@pytest.mark.asyncio
async def test_no_matching_rule_escalates_and_continues_down_chosen_branch(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    """Scenario 2 — no declarative rule matches, the LLM escalation picks
    `b`, and the run continues into it."""
    tpl_a = await _create_template(app_session_factory, config=_EMITS_STATUS_CONFIG)
    tpl_b = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    provider = MockProvider(
        "mock",
        [
            _completion(json.dumps({"status": "in_progress"})),  # node a's own output
            _completion(json.dumps({"target": "b", "reason": "keep going"})),  # escalation
            _completion(json.dumps({"status": "final"})),  # node b's own output
        ],
    )
    app.state.llm_router = LLMRouter(providers={"mock": provider}, default_chain=["mock"])
    wire_execution_service(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "e2e-escalates",
                "nodes": [
                    {"node_id": "a", "agent_template_id": str(tpl_a.id)},
                    {"node_id": "b", "agent_template_id": str(tpl_b.id)},
                ],
                "edges": [
                    {
                        "from_node_id": "a",
                        "to_node_id": "b",
                        "condition": "output.status == 'never'",
                    }
                ],
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        workflow_id = create_resp.json()["workflow_id"]

        run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {}},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["run_id"]

        row = await _poll_run_status(seed_session_factory, run_id, terminal={"completed", "error"})

    assert row["status"] == "completed", row
    assert row["checkpoint"]["node_statuses"] == {"a": "success", "b": "success"}
    routing = row["metrics"]["routing"]
    assert routing["deterministic"] == 0
    assert routing["llm_escalated"] == 1
    # IG1 — the escalation's own spend, which used to be dropped on the floor:
    # attributed to the run's totals AND kept separable from node spend, so
    # "routing deterministically is cheaper" stays a falsifiable claim.
    assert routing["tokens"] == {"input": 10, "output": 5}
    assert routing["cost_usd"] is not None
    assert Decimal(routing["cost_usd"]) > 0
    # And it is actually INCLUDED in the run total, not merely reported beside it.
    assert Decimal(row["metrics"]["total_cost_usd"]) >= Decimal(routing["cost_usd"])
    node_input = sum(n["input_tokens"] for n in row["metrics"]["per_node"].values())
    assert row["metrics"]["total_tokens"]["input"] == node_input + 10

    decision = row["checkpoint"]["routing_decisions"]["a"]
    assert decision["mode"] == "llm_escalated"
    assert decision["targets"] == ["b"]
    assert decision["llm_model"] == "claude-haiku-4-5"
    assert decision["llm_input_tokens"] == 10
    assert decision["llm_output_tokens"] == 5

    escalated_count = await _count_outbox(
        seed_session_factory, "workflow_engine.workflow_run.routing_escalated", run_id=run_id
    )
    assert escalated_count == 1

    async with seed_session_factory() as session:
        stats_resp_row = await session.execute(
            text(
                "SELECT payload FROM outbox_events "
                "WHERE event_type = 'workflow_engine.workflow_run.routing_escalated' "
                "AND payload->>'run_id' = :r"
            ),
            {"r": run_id},
        )
        payload = stats_resp_row.mappings().one()["payload"]
    assert payload["node_id"] == "a"
    assert payload["decision_target"] == ["b"]
    assert "status" not in json.dumps(payload.get("context", {}))  # no own_output leakage


@pytest.mark.asyncio
async def test_routing_stats_endpoint_reports_ratio_and_null_for_empty_workflow(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    """Scenario 3 — `GET /workflows/{id}/routing-stats`."""
    tpl_a = await _create_template(app_session_factory, config=_EMITS_STATUS_CONFIG)
    tpl_b = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    provider = MockProvider(
        "mock",
        [
            _completion(json.dumps({"status": "in_progress"})),
            _completion(json.dumps({"target": "b", "reason": "keep going"})),
            _completion(json.dumps({"status": "final"})),
        ],
    )
    app.state.llm_router = LLMRouter(providers={"mock": provider}, default_chain=["mock"])
    wire_execution_service(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "e2e-routing-stats",
                "nodes": [
                    {"node_id": "a", "agent_template_id": str(tpl_a.id)},
                    {"node_id": "b", "agent_template_id": str(tpl_b.id)},
                ],
                "edges": [
                    {
                        "from_node_id": "a",
                        "to_node_id": "b",
                        "condition": "output.status == 'never'",
                    }
                ],
            },
        )
        workflow_id = create_resp.json()["workflow_id"]

        run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {}},
        )
        run_id = run_resp.json()["run_id"]
        await _poll_run_status(seed_session_factory, run_id, terminal={"completed", "error"})

        stats_resp = await client.get(
            f"/api/v1/workflows/{workflow_id}/routing-stats", headers=_auth_headers()
        )
        assert stats_resp.status_code == 200, stats_resp.text
        stats = stats_resp.json()
        assert stats["workflow_id"] == workflow_id
        assert stats["runs_counted"] == 1
        assert stats["deterministic"] == 0
        assert stats["llm_escalated"] == 1
        assert stats["deterministic_pct"] == 0.0

        # An empty (no run) workflow reports `null`, never a ZeroDivisionError.
        empty_create_resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "e2e-routing-stats-empty",
                "nodes": [{"node_id": "a", "agent_template_id": str(tpl_a.id)}],
                "edges": [],
            },
        )
        empty_workflow_id = empty_create_resp.json()["workflow_id"]
        empty_stats_resp = await client.get(
            f"/api/v1/workflows/{empty_workflow_id}/routing-stats", headers=_auth_headers()
        )
        assert empty_stats_resp.status_code == 200
        empty_stats = empty_stats_resp.json()
        assert empty_stats["runs_counted"] == 0
        assert empty_stats["deterministic_pct"] is None

        # 404 on an unknown workflow_id (URL's primary resource).
        unknown_resp = await client.get(
            f"/api/v1/workflows/{uuid4()}/routing-stats", headers=_auth_headers()
        )
        assert unknown_resp.status_code == 404

        # ─── AC3 bullet 5 / T11.8 — legacy and corrupted rows ──────────
        # The `jsonb_typeof` guard is the one thing AC3 singles out as
        # needing proof, and it can only be proven against real Postgres:
        # the repo unit tests mock the session, so the SQL never executes
        # there. These rows are shaped exactly like what is already in a dev
        # database (Story 4.1/4.2 runs with no `routing` key) plus the
        # corruption modes the cast can actually hit.
        async with seed_session_factory() as session:
            for metrics in (
                '{"total_cost_usd": "0.01"}',  # legacy 4.1/4.2 row: no `routing`
                '{"routing": {}}',  # `routing` present but empty
                '{"routing": {"deterministic": "oops", "llm_escalated": null}}',  # non-numeric
                '{"routing": {"deterministic": 1.5, "llm_escalated": 2.9}}',  # fractional
            ):
                await session.execute(
                    text(
                        "INSERT INTO workflow_runs "
                        "(id, workflow_id, status, correlation_id, metrics) "
                        "VALUES (:id, :wf, 'completed', :cid, CAST(:m AS jsonb))"
                    ),
                    {"id": uuid4(), "wf": workflow_id, "cid": uuid4(), "m": metrics},
                )
            await session.commit()

        mixed_resp = await client.get(
            f"/api/v1/workflows/{workflow_id}/routing-stats", headers=_auth_headers()
        )
        assert mixed_resp.status_code == 200, mixed_resp.text
        mixed = mixed_resp.json()
        assert mixed["runs_counted"] == 5
        # Only the fractional row contributes: floor(1.5) + the real run's 0.
        assert mixed["deterministic"] == 1
        # floor(2.9) = 2, plus the real escalation from the run above.
        assert mixed["llm_escalated"] == 3
