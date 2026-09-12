"""End-to-end integration test — handoff summaries (Story 4.7 T8.9).

Real Postgres, real ``AsyncPostgresSaver`` checkpointing, mocked LLM
(``MockProvider`` — no network calls). Exercises the full path: a linear
``a -> b -> c`` run where ``a`` and ``b`` each get their output condensed
into a :class:`HandoffSummary` for their successor (AC1/AC2), ``c`` opts out
via ``include_raw_previous_output=true`` and gets ``b``'s raw output instead
(AC2), and ``GET /workflows/{id}/handoff-stats`` reflects the resulting
token reduction (AC3).

Honest limit (Dev Notes § Limite de test honnête): the ≥30% target is a
design goal, not something a `MockProvider`-backed test can validate for a
REAL summary. This test proves the ARITHMETIC (`reduction_ratio_pct`
computed correctly from real `Completion.output_tokens`) and the WIRING
(the summary, not the raw output, is what reaches the next node's prompt) —
never the quality of an actual LLM-produced summary. The token sizes below
are chosen to illustrate the ratio, not to claim it for real workflows.
"""

from __future__ import annotations

import asyncio
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


def _completion(text_: str, *, output_tokens: int = 5, input_tokens: int = 10) -> Completion:
    return Completion(
        text=text_,
        model="mock-model",
        provider="mock",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_reason="stop",
        latency_ms=5.0,
        cost_estimate_usd=Decimal("0.0001"),
    )


async def _create_template(
    session_factory: async_sessionmaker[AsyncSession], *, config: dict[str, Any] | None = None
) -> Any:
    repo = AgentTemplateRepo(session_factory=session_factory)
    return await repo.create(
        name=f"tpl-{uuid4()}", archetype="producteur", config=config if config is not None else {}
    )


async def _poll_run_status(
    factory: async_sessionmaker[AsyncSession], run_id: str, *, terminal: set[str]
) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + _POLL_TIMEOUT_S
    while asyncio.get_event_loop().time() < deadline:
        async with factory() as session:
            result = await session.execute(
                text("SELECT status, checkpoint, metrics FROM workflow_runs WHERE id = :id"),
                {"id": run_id},
            )
            row = result.mappings().one()
        if row["status"] in terminal:
            return dict(row)
        await asyncio.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"run {run_id} did not reach a terminal status within {_POLL_TIMEOUT_S}s")


# A deliberately verbose raw output, standing in for a real agent's full
# output — the "before" side of the reduction this story exists to produce.
_A_RAW_TEXT = '{"step": "a", "verbose": "' + ("detail " * 40) + '"}'
_A_SUMMARY_TEXT = (
    '{"decisions": ["chose plan A"], "artifacts_refs": [], "blockers": [], "next_questions": []}'
)
_B_RAW_TEXT = '{"step": "b"}'
_C_RAW_TEXT = '{"step": "c"}'


@pytest.mark.asyncio
async def test_handoff_summary_e2e_linear_chain(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    tpl_a = await _create_template(app_session_factory)
    tpl_b = await _create_template(app_session_factory)
    # AC2 opt-out: `c` is the run's LAST node (no downstream of its own,
    # T3.1 default), and reads `node_outputs` in full instead of `handoffs`.
    tpl_c = await _create_template(
        app_session_factory, config={"include_raw_previous_output": True}
    )

    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    # FIFO order: a's own completion, a's handoff summary, b's own
    # completion, c's own completion.
    #
    # `b` is NOT summarized, and that is the point (review of 2026-09-12,
    # I-01): its only successor is `c`, which opted out of summaries — so a
    # summary of `b` is something nobody in this DAG could ever read.
    # `has_downstream` used to answer the structural question ("does `b`
    # have a successor?") instead of the one AC1 actually asks ("will
    # anyone read it?"), so this run paid a fifth LLM call every time, and
    # the opt-out INCREASED the bill rather than reducing it.
    provider = MockProvider(
        "mock",
        [
            _completion(_A_RAW_TEXT, output_tokens=1000),
            _completion(_A_SUMMARY_TEXT, output_tokens=100),
            _completion(_B_RAW_TEXT, output_tokens=60),
            _completion(_C_RAW_TEXT, output_tokens=5),
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
                "name": "e2e-handoff-chain",
                "nodes": [
                    {"node_id": "a", "agent_template_id": str(tpl_a.id)},
                    {"node_id": "b", "agent_template_id": str(tpl_b.id)},
                    {"node_id": "c", "agent_template_id": str(tpl_c.id)},
                ],
                "edges": [
                    {"from_node_id": "a", "to_node_id": "b"},
                    {"from_node_id": "b", "to_node_id": "c"},
                ],
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        workflow_id = create_resp.json()["workflow_id"]

        run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {"question": "hello"}},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["run_id"]

        row = await _poll_run_status(seed_session_factory, run_id, terminal={"completed", "error"})
        assert row["status"] == "completed", row

        stats_resp = await client.get(
            f"/api/v1/workflows/{workflow_id}/handoff-stats", headers=_auth_headers()
        )

    assert stats_resp.status_code == 200, stats_resp.text

    # AC1/AC2 wiring — every completion call's OWN prompt content, in FIFO
    # order: [a, a-summary, b, b-summary, c].
    calls = provider.calls
    assert len(calls) == 4

    # `b`'s prompt (default: prefer handoffs) carries `a`'s SUMMARY, never
    # its verbose raw text.
    b_prompt = calls[2]["messages"][0].content
    assert "chose plan A" in b_prompt
    assert "detail" not in b_prompt

    # `c`'s prompt (opted out via `include_raw_previous_output=true`) carries
    # the RAW output of EVERY upstream node, never a summary — including
    # `a`'s, whose summary does exist in `handoffs`. The opt-out is
    # all-or-nothing for the consuming node, not per-predecessor.
    c_prompt = calls[3]["messages"][0].content
    assert '"step": "b"' in c_prompt
    assert "detail" in c_prompt
    assert "chose plan A" not in c_prompt

    # AC1 — the applicative checkpoint reflects `handoffs` for both
    # non-terminal nodes, keyed by producer, never for `c`.
    handoffs = row["checkpoint"]["handoffs"]
    # I-01 — `a` only: `b`'s sole successor opted out, so `b` was never
    # summarized, and `c` is terminal.
    assert set(handoffs) == {"a"}
    assert handoffs["a"]["decisions"] == ["chose plan A"]

    # B-01 — the CONSUMER-side channel, keyed by consumer, recording what was
    # really substituted. `b` replaced `a`'s raw output with its summary;
    # `c` opted out and replaced nothing, so it records nothing at all —
    # the producer-side count used to credit `c`'s hop as a saving anyway.
    substitutions = row["checkpoint"]["handoff_substitutions"]
    assert set(substitutions) == {"b"}
    assert substitutions["b"] == {
        "raw_tokens_replaced": 1000,
        "summary_tokens": 100,
        "sources": ["a"],
    }

    # AC3 — the ratio is computed from REAL measured tokens, and counted at
    # the point of SUBSTITUTION (B-01): `a`'s raw 1000 was replaced by its
    # 100-token summary in `b`'s prompt, once. `c` opted out, so its hop
    # contributes nothing. 90% reduction, comfortably illustrating >= 30%
    # WITHOUT claiming to validate a real summary's quality (Dev Notes
    # § Limite de test honnête) — the numbers are the test's own fixture,
    # not a model's judgment.
    metrics = row["metrics"]
    assert metrics["handoffs"] == {
        "raw_tokens_replaced": 1000,
        "summary_tokens": 100,
        "reduction_ratio": round(1 - 100 / 1000, 4),
        # Review of 2026-09-12 (P-2) — the FULL spend of the two summary
        # calls (input side included), which the ratio above deliberately
        # excludes. Two different questions, two different numbers.
        "tokens": {"input": 10, "output": 100},
        "cost_usd": "0.0001",
    }
    assert metrics["handoffs"]["reduction_ratio"] >= 0.30

    # P-2 — and that spend is IN the run's totals, not merely reported beside
    # them. Before the fix, this run billed 5 LLM calls and reported 3: the
    # `total_tokens`/`total_cost_usd` an operator budgets against diverged
    # permanently from the provider invoice, by one call per non-terminal
    # node. `_aggregate_routing` had exactly this bug fixed before it (IG1);
    # this is the same invariant, applied to Story 4.7's own extra call.
    node_only_input = sum(n["input_tokens"] for n in metrics["per_node"].values())
    node_only_output = sum(n["output_tokens"] for n in metrics["per_node"].values())
    assert metrics["total_tokens"] == {
        "input": node_only_input + 10,
        "output": node_only_output + 100,
    }
    # And per-node, the cost of each producer's own summary is queryable —
    # absent, not zeroed, for a node that was never summarized.
    assert metrics["per_node"]["a"]["handoff_summary_tokens"]["output"] == 100
    assert metrics["per_node"]["a"]["handoff_summary_model"] == "mock-model"
    assert metrics["per_node"]["b"].get("handoff_summary_tokens") is None
    assert metrics["per_node"]["c"].get("handoff_summary_tokens") is None

    # P-4 — the engine's token bookkeeping NEVER reaches a consuming agent's
    # prompt: `b` sees `a`'s four contract fields, never the accounting keys
    # that travel in the same `handoffs` entry.
    for leaked in ("raw_output_tokens_replaced", "summary_input_tokens", "summary_cost_usd"):
        assert leaked not in b_prompt

    stats = stats_resp.json()
    assert stats["workflow_id"] == workflow_id
    assert stats["runs_counted"] == 1
    assert stats["raw_tokens_replaced"] == 1000
    assert stats["summary_tokens"] == 100
    assert stats["reduction_ratio_pct"] == round((1 - 100 / 1000) * 100, 1)
