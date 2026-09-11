"""End-to-end integration test — Dry Run predictif (Story 4.4 T6.4).

Real Postgres. ``DryRunService`` needs no ``llm_router``/checkpointer/
``routing_rules`` at all (AC1's structural "zero LLM call" guarantee) — only
the two scenarios that need a REAL past run (to populate history) wire
``wire_execution_service`` + ``MockProvider`` to actually execute one via the
existing ``POST /workflows/{id}/runs`` endpoint first.

Scenarios (T6.4):
1. A workflow with a real, completed run reflects that run's ``per_node``
   token history in the Dry Run estimate.
2. A workflow whose real run recorded a ``routing_decisions`` entry has that
   decision replayed by the Dry Run's path prediction (review fix P13 —
   T6.4 asked for this and scenario 1's single-node DAG could not provide
   it: with no edges there is no decision point to record).
3. A freshly created workflow with zero runs falls back to the configured
   heuristic and reports ``no_execution_history``.
4. ``workflow.status != "active"`` → 422.
5. Unknown ``workflow_id`` → 404.

The ``_no_external_http`` autouse fixture (``tests/conftest.py``) already
proves "no real network call" for every test in this module — including
Dry Run's stronger "no LLM call at all" claim.
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


async def _poll_run_status(
    factory: async_sessionmaker[AsyncSession], run_id: str, *, terminal: set[str]
) -> dict[str, Any]:
    # `get_running_loop()`, not `get_event_loop()` (review fix P13): the
    # latter is deprecated and, from 3.12, only happens to work because a
    # loop is already running here.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _POLL_TIMEOUT_S
    while loop.time() < deadline:
        async with factory() as session:
            result = await session.execute(
                # `checkpoint`/`metrics` too (review fix P13): asserting on
                # the real persisted shape the Dry Run consumes is the whole
                # point of these scenarios. Mirrors the same helper in
                # `test_hybrid_routing_e2e.py`.
                text("SELECT status, checkpoint, metrics FROM workflow_runs WHERE id = :id"),
                {"id": run_id},
            )
            row = result.mappings().one()
        if row["status"] in terminal:
            return dict(row)
        await asyncio.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"run {run_id} did not reach a terminal status within {_POLL_TIMEOUT_S}s")


@pytest.mark.asyncio
async def test_dry_run_reflects_real_run_history(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    # An EXPLICIT `llm_model` (review fix P13): with `config={}` the template
    # fell through to `_DEFAULT_LLM_MODEL` ("claude-sonnet-4-6") while the
    # real run executed on "claude-haiku-4-5", so the assertion below on the
    # "anthropic" bucket held with pricing off by 3.75x. Pinning the model
    # the run actually uses makes the cost assertion mean something.
    tpl = await _create_template(app_session_factory, config={"llm_model": "claude-haiku-4-5"})
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
                "name": "e2e-dry-run-with-history",
                "nodes": [{"node_id": "a", "agent_template_id": str(tpl.id)}],
                "edges": [],
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
        # Review fix P13 — the run must have SUCCEEDED, not merely settled.
        # Accepting `error` here meant a run that failed before writing
        # `metrics.per_node` surfaced as a `KeyError: 'anthropic'` or a bare
        # `10 != 500` far below, instead of saying the run failed. This test
        # is about real history, so the run producing it has to be real.
        assert row["status"] == "completed", f"seed run ended {row['status']!r}, expected completed"

        dry_run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/dry-run",
            headers=_auth_headers(),
            json={"input": {}},
        )

    assert dry_run_resp.status_code == 200, dry_run_resp.text
    body = dry_run_resp.json()
    assert body["workflow_id"] == workflow_id
    assert body["probable_path"] == ["a"]
    assert {a["node_id"] for a in body["agents_involved"]} == {"a"}
    # The node's own completion cost 10 input / 5 output tokens (`_completion`
    # above) — the Dry Run's historical average must match exactly (one
    # sample, average == the sample).
    estimate = body["token_estimate_per_provider"]["anthropic"]
    assert estimate["input_tokens"] == 10
    assert estimate["output_tokens"] == 5
    # Haiku at 10 in / 5 out = 10/1e6*0.80 + 5/1e6*4.00 = 0.000028 USD.
    # Asserting the exact figure (review fix P13) rather than just
    # "not None" is what makes the pricing table, the provider grouping
    # and the historical averaging all actually verified end to end.
    assert estimate["cost_usd"] == "0.000028"
    assert body["cost_estimate_usd"] == "0.000028"
    risk_codes = {risk["code"] for risk in body["identified_risks"]}
    assert "no_execution_history" not in risk_codes

    # No real LLM call was made by the Dry Run itself: the provider only
    # ever saw the ONE call from actually running the workflow above.
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_dry_run_reflects_real_routing_decisions_from_history(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    """Review fix P13 — T6.4 asks that scenario 1 reflect real
    ``per_node`` **AND** ``routing_decisions``, but its workflow has a
    single node and zero edges: no decision point exists, so
    ``_routing_history_from_runs`` was never exercised against the shape
    Story 4.3 actually persists. Anything about that shape could have
    drifted and every test would still have passed.

    Here `a → b` carries a branching condition, so `a` IS a decision point.
    The shipped `terminal-output-status` rule resolves it deterministically
    to END, which is the interesting case: the Dry Run must read that
    recorded decision back, terminate `probable_path` at `a` WITHOUT
    flagging uncertainty — and still price `b`, which the estimate covers
    because a future run could reach it.
    """
    emits_status = {"output_contract": {"core": {"status": {"type": "string"}}}}
    tpl_a = await _create_template(
        app_session_factory, config={**emits_status, "llm_model": "claude-haiku-4-5"}
    )
    tpl_b = await _create_template(app_session_factory, config={"llm_model": "claude-haiku-4-5"})
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
                "name": "e2e-dry-run-routing-history",
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
        row = await _poll_run_status(
            seed_session_factory, run_resp.json()["run_id"], terminal={"completed", "error"}
        )

        dry_run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/dry-run",
            headers=_auth_headers(),
            json={"input": {}},
        )

    assert row["status"] == "completed", row
    # The real 4.3-persisted shape this Dry Run is about to consume.
    assert row["checkpoint"]["routing_decisions"]["a"]["mode"] == "deterministic"

    assert dry_run_resp.status_code == 200, dry_run_resp.text
    body = dry_run_resp.json()
    # The recorded decision terminated at END, so the path stops at `a`.
    assert body["probable_path"] == ["a"]
    codes_by_node = {(r["code"], r["node_id"]) for r in body["identified_risks"]}
    # A real majority WAS found for `a`, so no routing risk on it.
    assert ("routing_decision_uncertain", "a") not in codes_by_node
    assert ("routing_decision_multi_target", "a") not in codes_by_node
    # `b` never executed (the rule terminated the branch), so it has no
    # measured history and is priced from the heuristic — IG2 says that out
    # loud rather than letting a fabricated figure pass as measurement.
    assert ("node_estimate_from_fallback", "b") in codes_by_node
    # `b` is off the probable path but still priced — excluding it would
    # understate what a future run can cost (Dev Notes § Algorithme, point 4).
    involvement = {a["node_id"]: a["on_probable_path"] for a in body["agents_involved"]}
    assert involvement == {"a": True, "b": False}

    # Only the node's own completion — the Dry Run itself called nothing.
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_dry_run_fresh_workflow_falls_back_to_heuristic(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tpl = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "e2e-dry-run-fresh",
                "nodes": [{"node_id": "a", "agent_template_id": str(tpl.id)}],
                "edges": [],
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        workflow_id = create_resp.json()["workflow_id"]

        dry_run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/dry-run",
            headers=_auth_headers(),
            json={"input": {}},
        )

    assert dry_run_resp.status_code == 200, dry_run_resp.text
    body = dry_run_resp.json()
    assert body["probable_path"] == ["a"]
    risk_codes = {risk["code"] for risk in body["identified_risks"]}
    assert "no_execution_history" in risk_codes


@pytest.mark.asyncio
async def test_dry_run_inactive_workflow_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tpl = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "e2e-dry-run-inactive",
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
            f"/api/v1/workflows/{workflow_id}/dry-run",
            headers=_auth_headers(),
            json={"input": {}},
        )
    assert resp.status_code == 422
    assert resp.json()["type"] == "/errors/validation"


@pytest.mark.asyncio
async def test_dry_run_unknown_workflow_returns_404(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/workflows/{uuid4()}/dry-run",
            headers=_auth_headers(),
            json={"input": {}},
        )
    assert resp.status_code == 404
    assert resp.json()["type"] == "/errors/not-found"
