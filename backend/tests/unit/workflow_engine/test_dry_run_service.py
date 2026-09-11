"""Unit tests — :class:`DryRunService.dry_run` (Story 4.4 T3.6/T6.2).

Mock-driven (``WorkflowRepo``/``WorkflowRunRepo``/``AgentTemplateRepo``
mocked) — mirrors ``tests/unit/workflow_engine/test_service.py``. The
Postgres-real integration path (real run history) lives in
``tests/integration/workflow_engine/test_dry_run_e2e.py`` (T6.4).
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from agentive_backend.features.workflow_engine.dry_run import DryRunService, DryRunSettings
from agentive_backend.shared.exceptions import NotFoundError, ValidationError

#: Named so the fallback assertions cannot silently drift from the value
#: the fixture actually configures (review fix P3).
_FALLBACK_INPUT_TOKENS = 500
_FALLBACK_OUTPUT_TOKENS = 500


def _settings(
    *,
    history_limit: int = 20,
    fallback_input_tokens: int = _FALLBACK_INPUT_TOKENS,
    fallback_output_tokens: int = _FALLBACK_OUTPUT_TOKENS,
    budget_cap_usd: Decimal | None = None,
) -> DryRunSettings:
    return DryRunSettings(
        history_limit=history_limit,
        fallback_input_tokens=fallback_input_tokens,
        fallback_output_tokens=fallback_output_tokens,
        budget_cap_usd=budget_cap_usd,
    )


def _workflow(*, status: str = "active", dag: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        status=status,
        dag=dag if dag is not None else {"nodes": [], "edges": []},
        tenant_id=None,
    )


def _template(*, template_id: UUID | None = None, llm_model: str | None = None) -> SimpleNamespace:
    config: dict[str, Any] = {}
    if llm_model is not None:
        config["llm_model"] = llm_model
    return SimpleNamespace(id=template_id or uuid4(), archetype="producteur", config=config)


def _run(
    *,
    checkpoint: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    status: str = "completed",
) -> SimpleNamespace:
    """A past run. `status` defaults to `completed` because only completed
    runs feed the routing majority (IG5) — token metrics still read every
    status, so a test that cares about the difference sets it explicitly."""
    return SimpleNamespace(
        id=uuid4(),
        checkpoint=checkpoint or {},
        metrics=metrics or {},
        status=status,
    )


def _make_service(
    *, settings: DryRunSettings | None = None
) -> tuple[DryRunService, AsyncMock, AsyncMock, AsyncMock]:
    """Returns (service, workflow_repo, workflow_run_repo, template_repo)."""
    workflow_repo = AsyncMock()
    workflow_run_repo = AsyncMock()
    workflow_run_repo.list_by_workflow = AsyncMock(return_value=[])
    template_repo = AsyncMock()
    service = DryRunService(
        workflow_repo=workflow_repo,
        workflow_run_repo=workflow_run_repo,
        template_repo=template_repo,
        settings=settings or _settings(),
    )
    return service, workflow_repo, workflow_run_repo, template_repo


def _single_node_dag(template_id: UUID) -> dict[str, Any]:
    return {"nodes": [{"node_id": "a", "agent_template_id": str(template_id)}], "edges": []}


# ─── Structural "zero LLM call" guarantee ──────────────────────────────


def test_dry_run_service_constructor_has_no_llm_router_parameter() -> None:
    """Not a mock assertion — the class itself cannot be given an
    `LLMRouter`, so it cannot call one even by implementation error."""
    params = inspect.signature(DryRunService.__init__).parameters
    assert "llm_router" not in params


# ─── Errors ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_unknown_workflow_raises_not_found() -> None:
    service, workflow_repo, _run_repo, _trepo = _make_service()
    workflow_repo.require_by_id.side_effect = NotFoundError(detail="Workflow 'x' not found")

    with pytest.raises(NotFoundError):
        await service.dry_run(workflow_id=uuid4(), task_input={})


@pytest.mark.asyncio
async def test_dry_run_inactive_workflow_raises_422() -> None:
    service, workflow_repo, _run_repo, _trepo = _make_service()
    workflow_repo.require_by_id.return_value = _workflow(status="draft")

    with pytest.raises(ValidationError, match="not active"):
        await service.dry_run(workflow_id=uuid4(), task_input={})


# ─── No history — heuristic fallback ───────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_with_no_history_uses_fallback_tokens_and_flags_risk() -> None:
    template_id = uuid4()
    service, workflow_repo, workflow_run_repo, template_repo = _make_service(
        settings=_settings(fallback_input_tokens=111, fallback_output_tokens=222)
    )
    workflow_repo.require_by_id.return_value = _workflow(dag=_single_node_dag(template_id))
    workflow_run_repo.list_by_workflow.return_value = []
    template_repo.get_by_id.return_value = _template(
        template_id=template_id, llm_model="claude-sonnet-4-6"
    )

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    assert response.probable_path == ["a"]
    assert len(response.agents_involved) == 1
    assert response.token_estimate_per_provider["anthropic"].input_tokens == 111
    assert response.token_estimate_per_provider["anthropic"].output_tokens == 222
    risk_codes = {risk.code for risk in response.identified_risks}
    assert "no_execution_history" in risk_codes


# ─── With history — averaged estimate ──────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_with_history_averages_per_node_metrics() -> None:
    template_id = uuid4()
    service, workflow_repo, workflow_run_repo, template_repo = _make_service()
    workflow_repo.require_by_id.return_value = _workflow(dag=_single_node_dag(template_id))
    workflow_run_repo.list_by_workflow.return_value = [
        _run(metrics={"per_node": {"a": {"input_tokens": 100, "output_tokens": 50}}}),
        _run(metrics={"per_node": {"a": {"input_tokens": 200, "output_tokens": 150}}}),
    ]
    template_repo.get_by_id.return_value = _template(
        template_id=template_id, llm_model="claude-sonnet-4-6"
    )

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    estimate = response.token_estimate_per_provider["anthropic"]
    assert estimate.input_tokens == 150
    assert estimate.output_tokens == 100
    assert estimate.cost_usd is not None
    assert response.cost_estimate_usd is not None
    risk_codes = {risk.code for risk in response.identified_risks}
    assert "no_execution_history" not in risk_codes


# ─── Decision point without historical majority ────────────────────────


@pytest.mark.asyncio
async def test_dry_run_decision_point_without_history_is_flagged_uncertain() -> None:
    template_a, template_b, template_c = uuid4(), uuid4(), uuid4()
    dag = {
        "nodes": [
            {"node_id": "a", "agent_template_id": str(template_a)},
            {"node_id": "b", "agent_template_id": str(template_b)},
            {"node_id": "c", "agent_template_id": str(template_c)},
        ],
        "edges": [
            {"from_node_id": "a", "to_node_id": "b", "condition": "output.status == 'ok'"},
            {"from_node_id": "a", "to_node_id": "c", "condition": "output.status == 'ko'"},
        ],
    }
    service, workflow_repo, workflow_run_repo, template_repo = _make_service()
    workflow_repo.require_by_id.return_value = _workflow(dag=dag)
    workflow_run_repo.list_by_workflow.return_value = [_run()]  # non-empty, but no routing history

    async def _get_by_id(template_id: UUID, *, tenant_id: Any | None = None) -> SimpleNamespace:
        return _template(template_id=template_id, llm_model="claude-sonnet-4-6")

    template_repo.get_by_id = AsyncMock(side_effect=_get_by_id)

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    assert response.probable_path == ["a", "b"]
    uncertain = [r for r in response.identified_risks if r.code == "routing_decision_uncertain"]
    assert len(uncertain) == 1
    assert uncertain[0].node_id == "a"
    # every structurally reachable node is priced, not just the probable path
    assert {a.node_id for a in response.agents_involved} == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_dry_run_decision_point_follows_real_checkpoint_history() -> None:
    """End-to-end: `checkpoint["routing_decisions"]` from past runs (Story
    4.3's persisted shape) resolves the decision point via majority,
    without any `routing_decision_uncertain` risk."""
    template_a, template_b, template_c = uuid4(), uuid4(), uuid4()
    dag = {
        "nodes": [
            {"node_id": "a", "agent_template_id": str(template_a)},
            {"node_id": "b", "agent_template_id": str(template_b)},
            {"node_id": "c", "agent_template_id": str(template_c)},
        ],
        "edges": [
            {"from_node_id": "a", "to_node_id": "b", "condition": "output.status == 'ok'"},
            {"from_node_id": "a", "to_node_id": "c", "condition": "output.status == 'ko'"},
        ],
    }
    service, workflow_repo, workflow_run_repo, template_repo = _make_service()
    workflow_repo.require_by_id.return_value = _workflow(dag=dag)
    workflow_run_repo.list_by_workflow.return_value = [
        _run(checkpoint={"routing_decisions": {"a": {"targets": ["c"]}}}),
        _run(checkpoint={"routing_decisions": {"a": {"targets": ["c"]}}}),
        _run(checkpoint={"routing_decisions": {"a": {"targets": ["b"]}}}),
    ]

    async def _get_by_id(template_id: UUID, *, tenant_id: Any | None = None) -> SimpleNamespace:
        return _template(template_id=template_id, llm_model="claude-sonnet-4-6")

    template_repo.get_by_id = AsyncMock(side_effect=_get_by_id)

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    assert response.probable_path == ["a", "c"]
    # `a` has a clean 2-1 historical majority for `c`, so NO routing risk.
    routing_codes = {
        risk.code
        for risk in response.identified_risks
        if risk.code.startswith("routing_") or risk.code == "no_execution_history"
    }
    assert routing_codes == set()
    # These runs carry `routing_decisions` but no `metrics.per_node`, so
    # every node is priced from the heuristic — which IG2 now says out
    # loud instead of presenting those figures as measured. Asserting
    # `identified_risks == []` here is what used to hide it.
    assert {risk.node_id for risk in response.identified_risks} == {"a", "b", "c"}
    assert {risk.code for risk in response.identified_risks} == {"node_estimate_from_fallback"}


@pytest.mark.asyncio
async def test_dry_run_tolerates_legacy_and_corrupted_run_shapes() -> None:
    """Pre-4.3 runs (no `routing_decisions` key at all) and corrupted rows
    must be skipped, never raise (Story 4.4 Dev Notes § Pièges connus #4)."""
    template_id = uuid4()
    service, workflow_repo, workflow_run_repo, template_repo = _make_service()
    workflow_repo.require_by_id.return_value = _workflow(dag=_single_node_dag(template_id))
    workflow_run_repo.list_by_workflow.return_value = [
        _run(checkpoint=None, metrics={}),  # pre-4.2 shape
        _run(checkpoint={}, metrics={"per_node": "not-a-dict"}),  # corrupted
        _run(
            checkpoint={"routing_decisions": "not-a-dict"},
            metrics={"per_node": {"a": {"input_tokens": "not-an-int", "output_tokens": 50}}},
        ),
    ]
    template_repo.get_by_id.return_value = _template(
        template_id=template_id, llm_model="claude-sonnet-4-6"
    )

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    assert response.probable_path == ["a"]
    # `input_tokens` was corrupted (non-int) on the only real sample, so the
    # average has no usable input value — but `output_tokens` (50) is valid
    # and independently averaged, per `average_node_tokens`' field-level
    # tolerance (Story 4.4 Dev Notes).
    estimate = response.token_estimate_per_provider["anthropic"]
    assert estimate.output_tokens == 50
    # Review fix P3 — the corrupted field falls back to the CONFIGURED
    # heuristic, not to a silent 0. This assertion is the whole point: the
    # earlier version of this test checked `output_tokens` only, which let a
    # fabricated `input_tokens == 0` through unnoticed.
    assert estimate.input_tokens == _FALLBACK_INPUT_TOKENS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("corrupt_value", "label"),
    [
        (True, "a JSON boolean (bool is an int in Python — counted as 1 token)"),
        (-5, "a negative count (rejected by ProviderTokenEstimate's ge=0 → bare 500)"),
        (10**30, "an absurd magnitude (overflows the Decimal context → bare 500)"),
    ],
)
async def test_dry_run_when_history_holds_implausible_token_counts_should_use_fallback(
    corrupt_value: object, label: str
) -> None:
    """Regression, review fix P2 — three JSONB shapes that `isinstance(int)`
    alone waved through. Each must be treated as absent data (so the
    configured fallback applies), never raise, and never be counted."""
    template_id = uuid4()
    service, workflow_repo, workflow_run_repo, template_repo = _make_service()
    workflow_repo.require_by_id.return_value = _workflow(dag=_single_node_dag(template_id))
    workflow_run_repo.list_by_workflow.return_value = [
        _run(metrics={"per_node": {"a": {"input_tokens": corrupt_value, "output_tokens": 50}}})
    ]
    template_repo.get_by_id.return_value = _template(
        template_id=template_id, llm_model="claude-sonnet-4-6"
    )

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    estimate = response.token_estimate_per_provider["anthropic"]
    assert estimate.input_tokens == _FALLBACK_INPUT_TOKENS, label
    assert estimate.output_tokens == 50


# ─── Budget cap (AC3) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_cost_exceeding_cap_flags_budget_cap_exceeded() -> None:
    template_id = uuid4()
    service, workflow_repo, workflow_run_repo, template_repo = _make_service(
        settings=_settings(budget_cap_usd=Decimal("0.0001"))
    )
    workflow_repo.require_by_id.return_value = _workflow(dag=_single_node_dag(template_id))
    workflow_run_repo.list_by_workflow.return_value = [
        _run(metrics={"per_node": {"a": {"input_tokens": 1_000_000, "output_tokens": 1_000_000}}})
    ]
    template_repo.get_by_id.return_value = _template(
        template_id=template_id, llm_model="claude-opus-4-7"
    )

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    risk_codes = {risk.code for risk in response.identified_risks}
    assert "budget_cap_exceeded" in risk_codes
    # Review fix P15 — T6.2 asks for "présent AVEC LE BON MONTANT", and AC3
    # for "le montant estimé et le seuil dépassé". Asserting only the code
    # let the two numbers be anything at all. Opus at 1M in + 1M out is
    # 15.00 + 75.00 = 90.00 USD, which must be both the reported estimate
    # and the amount named in the risk.
    assert response.cost_estimate_usd == "90.000000"
    risk = next(r for r in response.identified_risks if r.code == "budget_cap_exceeded")
    # BS1 — both figures as FIELDS, so Epic 6's Dialog reads a number
    # instead of parsing an English sentence to gate a button on it.
    assert risk.estimated_usd == "90.000000"
    assert risk.threshold_usd == "0.0001"
    # `detail` still states both, for a human reading the raw response.
    assert "90.000000" in risk.detail
    assert "0.0001" in risk.detail


@pytest.mark.asyncio
async def test_dry_run_never_flags_budget_cap_when_unset() -> None:
    """`budget_cap_usd=None` (default) disables the check regardless of cost."""
    template_id = uuid4()
    service, workflow_repo, workflow_run_repo, template_repo = _make_service(
        settings=_settings(budget_cap_usd=None)
    )
    workflow_repo.require_by_id.return_value = _workflow(dag=_single_node_dag(template_id))
    workflow_run_repo.list_by_workflow.return_value = [
        _run(metrics={"per_node": {"a": {"input_tokens": 1_000_000, "output_tokens": 1_000_000}}})
    ]
    template_repo.get_by_id.return_value = _template(
        template_id=template_id, llm_model="claude-opus-4-7"
    )

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    risk_codes = {risk.code for risk in response.identified_risks}
    assert "budget_cap_exceeded" not in risk_codes


# ─── Model absent from every pricing table ─────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_unresolvable_model_yields_none_cost_without_raising() -> None:
    template_id = uuid4()
    service, workflow_repo, workflow_run_repo, template_repo = _make_service()
    workflow_repo.require_by_id.return_value = _workflow(dag=_single_node_dag(template_id))
    workflow_run_repo.list_by_workflow.return_value = []
    template_repo.get_by_id.return_value = _template(
        template_id=template_id, llm_model="some-unknown-model"
    )

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    assert response.token_estimate_per_provider == {}
    assert response.cost_estimate_usd is None


# ─── Review intent gaps (IG1-IG7) ───────────────────────────────────────


def _branching_dag(a: UUID, b: UUID, c: UUID) -> dict[str, Any]:
    """`a` is a decision point fanning out to `b` and `c`."""
    return {
        "nodes": [
            {"node_id": "a", "agent_template_id": str(a)},
            {"node_id": "b", "agent_template_id": str(b)},
            {"node_id": "c", "agent_template_id": str(c)},
        ],
        "edges": [
            {"from_node_id": "a", "to_node_id": "b", "condition": "output.status == 'ok'"},
            {"from_node_id": "a", "to_node_id": "c", "condition": "output.status == 'ko'"},
        ],
    }


def _every_template(llm_model: str | None = "claude-sonnet-4-6") -> AsyncMock:
    async def _get_by_id(template_id: UUID, *, tenant_id: Any | None = None) -> SimpleNamespace:
        return _template(template_id=template_id, llm_model=llm_model)

    return AsyncMock(side_effect=_get_by_id)


@pytest.mark.asyncio
async def test_dry_run_when_a_model_has_no_price_should_flag_it_and_bound_the_total() -> None:
    """IG1 — an unpriced node used to vanish from BOTH the token figures and
    the cost with nothing but a server-side log to show for it, so the
    response looked complete while covering only part of the workflow."""
    template_id = uuid4()
    service, workflow_repo, _run_repo, template_repo = _make_service()
    workflow_repo.require_by_id.return_value = _workflow(dag=_single_node_dag(template_id))
    template_repo.get_by_id.return_value = _template(
        template_id=template_id, llm_model="gpt-6-does-not-exist"
    )

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    assert response.cost_estimate_usd is None
    assert response.token_estimate_per_provider == {}
    risk = next(r for r in response.identified_risks if r.code == "model_price_unresolved")
    assert risk.node_id == "a"
    assert "gpt-6-does-not-exist" in risk.detail


@pytest.mark.asyncio
async def test_dry_run_when_cap_exceeded_on_a_partial_total_should_say_so() -> None:
    """IG1 — the budget cap compares against a total that may be missing
    unpriced nodes. Saying nothing made the cap look authoritative."""
    priced, unpriced = uuid4(), uuid4()
    dag = {
        "nodes": [
            {"node_id": "a", "agent_template_id": str(priced)},
            {"node_id": "b", "agent_template_id": str(unpriced)},
        ],
        "edges": [{"from_node_id": "a", "to_node_id": "b"}],
    }
    service, workflow_repo, _run_repo, template_repo = _make_service(
        settings=_settings(budget_cap_usd=Decimal("0.0000001"))
    )
    workflow_repo.require_by_id.return_value = _workflow(dag=dag)

    async def _get_by_id(template_id: UUID, *, tenant_id: Any | None = None) -> SimpleNamespace:
        model = "claude-sonnet-4-6" if template_id == priced else "mystery-model"
        return _template(template_id=template_id, llm_model=model)

    template_repo.get_by_id = AsyncMock(side_effect=_get_by_id)

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    codes = {risk.code for risk in response.identified_risks}
    assert {"budget_cap_exceeded", "model_price_unresolved"} <= codes
    cap_risk = next(r for r in response.identified_risks if r.code == "budget_cap_exceeded")
    assert "incomplete" in cap_risk.detail


@pytest.mark.asyncio
async def test_dry_run_when_workflow_ran_but_node_did_not_should_flag_that_node() -> None:
    """IG2 — `no_execution_history` only ever fired for a workflow that had
    never run at all. A node added to a long-running workflow was priced
    from the heuristic and presented as measured."""
    a, b, c = uuid4(), uuid4(), uuid4()
    service, workflow_repo, workflow_run_repo, template_repo = _make_service()
    workflow_repo.require_by_id.return_value = _workflow(dag=_branching_dag(a, b, c))
    workflow_run_repo.list_by_workflow.return_value = [
        _run(
            checkpoint={"routing_decisions": {"a": {"targets": ["b"]}}},
            metrics={"per_node": {"a": {"input_tokens": 10, "output_tokens": 5}}},
        )
    ]
    template_repo.get_by_id = _every_template()

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    codes = {risk.code for risk in response.identified_risks}
    assert "no_execution_history" not in codes  # the workflow HAS run
    flagged = {
        r.node_id for r in response.identified_risks if r.code == "node_estimate_from_fallback"
    }
    assert flagged == {"b", "c"}  # `a` was measured, the other two were not


@pytest.mark.asyncio
async def test_dry_run_should_flag_uncertain_decision_points_off_the_probable_path() -> None:
    """IG3 — uncertainty used to be collected during the path walk only, so
    a workflow whose uncertainty sat on an untaken branch was reported as
    certain while still being billed for it."""
    a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
    dag = {
        "nodes": [
            {"node_id": "a", "agent_template_id": str(a)},
            {"node_id": "b", "agent_template_id": str(b)},
            {"node_id": "c", "agent_template_id": str(c)},
            {"node_id": "d", "agent_template_id": str(d)},
        ],
        "edges": [
            {"from_node_id": "a", "to_node_id": "b"},
            # `c` is a decision point reachable only via the fan-out, and it
            # has no history at all.
            {"from_node_id": "a", "to_node_id": "c"},
            {"from_node_id": "c", "to_node_id": "d", "condition": "output.status == 'ok'"},
        ],
    }
    service, workflow_repo, _run_repo, template_repo = _make_service()
    workflow_repo.require_by_id.return_value = _workflow(dag=dag)
    template_repo.get_by_id = _every_template()

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    # `a` is an unconditional fan-out, so the path goes a → b and never
    # visits `c`...
    assert response.probable_path == ["a", "b"]
    # ...but `c` is billed, so its uncertainty must be reported.
    uncertain = {
        r.node_id for r in response.identified_risks if r.code == "routing_decision_uncertain"
    }
    assert uncertain == {"c"}


@pytest.mark.asyncio
async def test_dry_run_when_history_fanned_out_should_flag_multi_target() -> None:
    """IG4 — a recorded decision naming several targets was narrowed to the
    first one silently, so the response implied a single branch where the
    engine would really run both."""
    a, b, c = uuid4(), uuid4(), uuid4()
    service, workflow_repo, workflow_run_repo, template_repo = _make_service()
    workflow_repo.require_by_id.return_value = _workflow(dag=_branching_dag(a, b, c))
    workflow_run_repo.list_by_workflow.return_value = [
        _run(checkpoint={"routing_decisions": {"a": {"targets": ["b", "c"]}}}),
        _run(checkpoint={"routing_decisions": {"a": {"targets": ["b", "c"]}}}),
    ]
    template_repo.get_by_id = _every_template()

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    multi = {
        r.node_id for r in response.identified_risks if r.code == "routing_decision_multi_target"
    }
    assert multi == {"a"}
    # The history is unambiguous, so this is NOT uncertainty.
    assert not [r for r in response.identified_risks if r.code == "routing_decision_uncertain"]


@pytest.mark.asyncio
async def test_dry_run_should_ignore_non_completed_runs_for_routing_majority() -> None:
    """IG5 — the story justified reading every run status for per-node token
    metrics, and that reasoning was silently reused for routing decisions.
    Three crashed runs must not define the predicted branch."""
    a, b, c = uuid4(), uuid4(), uuid4()
    service, workflow_repo, workflow_run_repo, template_repo = _make_service()
    workflow_repo.require_by_id.return_value = _workflow(dag=_branching_dag(a, b, c))
    workflow_run_repo.list_by_workflow.return_value = [
        _run(status="error", checkpoint={"routing_decisions": {"a": {"targets": ["c"]}}}),
        _run(status="error", checkpoint={"routing_decisions": {"a": {"targets": ["c"]}}}),
        _run(status="running", checkpoint={"routing_decisions": {"a": {"targets": ["c"]}}}),
        _run(status="completed", checkpoint={"routing_decisions": {"a": {"targets": ["b"]}}}),
    ]
    template_repo.get_by_id = _every_template()

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    # 3 crashed votes for `c` vs 1 healthy vote for `b` — the healthy one wins.
    assert response.probable_path == ["a", "b"]


@pytest.mark.asyncio
async def test_dry_run_should_include_historical_routing_escalation_cost() -> None:
    """IG6 — `checkpoint["routing_decisions"]` carries the LLM tokens an
    escalation spent, `_aggregate_metrics` adds them to the run's real
    totals (Story 4.3 review IG1), and the Dry Run loaded them then threw
    them away — comparing "nodes only" against a "nodes + escalations"
    reality."""
    a, b, c = uuid4(), uuid4(), uuid4()
    escalated = {
        "targets": ["b"],
        "llm_model": "claude-haiku-4-5",
        "llm_input_tokens": 1_000_000,
        "llm_output_tokens": 1_000_000,
    }
    service, workflow_repo, workflow_run_repo, template_repo = _make_service()
    workflow_repo.require_by_id.return_value = _workflow(dag=_branching_dag(a, b, c))
    workflow_run_repo.list_by_workflow.return_value = [
        _run(checkpoint={"routing_decisions": {"a": escalated}})
    ]
    template_repo.get_by_id = _every_template()

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    # Haiku at 1M in + 1M out = 0.80 + 4.00 = 4.80 USD of escalation, which
    # would have been invisible before.
    assert response.cost_estimate_usd is not None
    assert Decimal(response.cost_estimate_usd) > Decimal("4.80")


@pytest.mark.asyncio
async def test_dry_run_should_report_probable_path_cost_beside_the_upper_bound() -> None:
    """IG7 — a 2-way exclusive decision bills both branches in
    `cost_estimate_usd`, but a real run pays one. The expected figure was
    computable from `on_probable_path` and simply not reported."""
    a, b, c = uuid4(), uuid4(), uuid4()
    service, workflow_repo, workflow_run_repo, template_repo = _make_service()
    workflow_repo.require_by_id.return_value = _workflow(dag=_branching_dag(a, b, c))
    workflow_run_repo.list_by_workflow.return_value = [
        _run(checkpoint={"routing_decisions": {"a": {"targets": ["b"]}}})
    ]
    template_repo.get_by_id = _every_template()

    response = await service.dry_run(workflow_id=uuid4(), task_input={})

    assert response.probable_path == ["a", "b"]
    assert response.cost_estimate_usd is not None
    assert response.probable_path_cost_usd is not None
    upper = Decimal(response.cost_estimate_usd)
    expected = Decimal(response.probable_path_cost_usd)
    # 3 identical nodes billed vs the 2 on the path.
    assert expected < upper
    assert expected * 3 == upper * 2
