"""Unit tests — :func:`build_state_graph` (Story 4.2 T3.5).

No real LLM calls — each node's completion is served by a
:class:`~agentive_backend.shared.llm.testing.MockProvider` wired into a real
:class:`LLMRouter`. Running the compiled graph end-to-end (``ainvoke``) with
a mocked LLM proves the STRUCTURAL wiring (which nodes ran, which branch was
taken) through actual execution rather than introspecting LangGraph
internals.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agentive_backend.features.workflow_engine.domain.condition_dsl import parse
from agentive_backend.features.workflow_engine.domain.value_objects import (
    WorkflowDag,
    WorkflowEdge,
    WorkflowNode,
)
from agentive_backend.features.workflow_engine.engine.graph_builder import build_state_graph
from agentive_backend.features.workflow_engine.engine.hybrid_router import RoutingSettings
from agentive_backend.shared.llm.router import LLMRouter
from agentive_backend.shared.llm.testing import MockProvider
from agentive_backend.shared.llm.types import Completion


def _completion(text: str = "{}") -> Completion:
    return Completion(
        text=text,
        model="mock-model",
        provider="mock",
        input_tokens=10,
        output_tokens=5,
        finish_reason="stop",
        latency_ms=1.0,
    )


def _template() -> SimpleNamespace:
    return SimpleNamespace(config={})


def _template_with(config: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(config=config)


def _dag(nodes: list[str], edges: list[tuple[str, str, str | None]]) -> WorkflowDag:
    return WorkflowDag(
        nodes=tuple(WorkflowNode(node_id=n, agent_template_id=uuid4()) for n in nodes),
        edges=tuple(WorkflowEdge(from_node_id=f, to_node_id=t, condition=c) for f, t, c in edges),
    )


def _initial_state() -> dict[str, Any]:
    return {"task_input": {}, "correlation_id": "cid", "node_outputs": {}, "node_metrics": {}}


def _routing_settings() -> RoutingSettings:
    """Story 4.3 — only decision-point nodes (>= 1 conditional outgoing
    edge) require this; every test with a purely unconditional DAG keeps
    the Story 4.2 call shape (AC4)."""
    return RoutingSettings(
        threshold=0.8,
        escalation_model="claude-haiku-4-5",
        escalation_timeout_s=15.0,
        escalation_max_tokens=256,
    )


@pytest.mark.asyncio
async def test_mono_node_dag_runs_and_terminates() -> None:
    dag = _dag(["a"], [])
    templates = {"a": _template()}
    router = LLMRouter(
        providers={"mock": MockProvider("mock", [_completion('{"x": 1}')])},
        default_chain=["mock"],
    )
    graph = build_state_graph(dag, templates, router).compile()

    result = await graph.ainvoke(_initial_state())

    assert result["node_outputs"] == {"a": {"x": 1}}


@pytest.mark.asyncio
async def test_linear_dag_runs_nodes_in_order() -> None:
    dag = _dag(
        ["a", "b", "c"],
        [("a", "b", None), ("b", "c", None)],
    )
    templates = {n: _template() for n in ("a", "b", "c")}
    router = LLMRouter(
        providers={
            "mock": MockProvider(
                "mock",
                [
                    _completion('{"step": "a"}'),
                    _completion('{"step": "b"}'),
                    _completion('{"step": "c"}'),
                ],
            )
        },
        default_chain=["mock"],
    )
    graph = build_state_graph(dag, templates, router).compile()

    result = await graph.ainvoke(_initial_state())

    assert result["node_outputs"] == {
        "a": {"step": "a"},
        "b": {"step": "b"},
        "c": {"step": "c"},
    }


@pytest.mark.asyncio
async def test_conditional_branch_only_true_target_runs() -> None:
    """2 conditional targets from `a`, only one condition evaluates True."""
    dag = _dag(
        ["a", "b", "c"],
        [
            ("a", "b", "output.status == 'ok'"),
            ("a", "c", "output.status == 'bad'"),
        ],
    )
    templates = {n: _template() for n in ("a", "b", "c")}
    router = LLMRouter(
        providers={
            "mock": MockProvider(
                "mock",
                [_completion('{"status": "ok"}')],
                infinite_default=_completion('{"reached": true}'),
            )
        },
        default_chain=["mock"],
    )
    graph = build_state_graph(
        dag, templates, router, rules=(), routing_settings=_routing_settings()
    ).compile()

    result = await graph.ainvoke(_initial_state())

    assert set(result["node_outputs"]) == {"a", "b"}
    assert "c" not in result["node_outputs"]


@pytest.mark.asyncio
async def test_conditional_and_unconditional_edge_same_node_both_taken() -> None:
    """A source node mixing one conditional (True) edge and one unconditional
    edge — LangGraph allows only ONE outgoing mechanism per source, so
    `build_state_graph` must route BOTH through the single conditional
    router (T3.2) rather than mixing `add_edge`/`add_conditional_edges`."""
    dag = _dag(
        ["a", "b", "c"],
        [
            ("a", "b", "output.status == 'ok'"),
            ("a", "c", None),
        ],
    )
    templates = {n: _template() for n in ("a", "b", "c")}
    router = LLMRouter(
        providers={
            "mock": MockProvider(
                "mock",
                [_completion('{"status": "ok"}')],
                infinite_default=_completion('{"reached": true}'),
            )
        },
        default_chain=["mock"],
    )
    graph = build_state_graph(
        dag, templates, router, rules=(), routing_settings=_routing_settings()
    ).compile()

    result = await graph.ainvoke(_initial_state())

    assert set(result["node_outputs"]) == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_multi_root_fan_out_both_roots_execute() -> None:
    """Two nodes with no incoming edge — both get `add_edge(START, ...)`."""
    dag = _dag(["a", "b"], [])
    templates = {n: _template() for n in ("a", "b")}
    router = LLMRouter(
        providers={
            "mock": MockProvider("mock", [], infinite_default=_completion('{"reached": true}'))
        },
        default_chain=["mock"],
    )
    graph = build_state_graph(dag, templates, router).compile()

    result = await graph.ainvoke(_initial_state())

    assert set(result["node_outputs"]) == {"a", "b"}


@pytest.mark.asyncio
async def test_multiple_sink_nodes_merge_via_reducer() -> None:
    """`a` fans out to two independent sinks `b`/`c` (unconditional, both
    taken). Without the `operator.or_` reducer on `node_outputs` (T3.3), one
    of the two concurrent partial updates would silently overwrite the
    other instead of merging."""
    dag = _dag(
        ["a", "b", "c"],
        [("a", "b", None), ("a", "c", None)],
    )
    templates = {n: _template() for n in ("a", "b", "c")}
    router = LLMRouter(
        providers={
            "mock": MockProvider(
                "mock",
                [_completion('{"status": "ok"}')],
                infinite_default=_completion('{"reached": true}'),
            )
        },
        default_chain=["mock"],
    )
    graph = build_state_graph(dag, templates, router).compile()

    result = await graph.ainvoke(_initial_state())

    assert set(result["node_outputs"]) == {"a", "b", "c"}


# ─── Story 4.3 T11.6 — hybrid routing wiring ──────────────────────────────


@pytest.mark.asyncio
async def test_decision_point_node_records_routing_decision_in_state() -> None:
    dag = _dag(["a", "b"], [("a", "b", "output.status == 'ok'")])
    templates = {n: _template() for n in ("a", "b")}
    router = LLMRouter(
        providers={
            "mock": MockProvider(
                "mock",
                [_completion('{"status": "ok"}'), _completion('{"reached": true}')],
            )
        },
        default_chain=["mock"],
    )
    graph = build_state_graph(
        dag, templates, router, rules=(), routing_settings=_routing_settings()
    ).compile()

    result = await graph.ainvoke(_initial_state())

    assert result["routing_decisions"]["a"]["source"] == "dsl"
    assert result["routing_decisions"]["a"]["targets"] == ["b"]
    assert "b" not in result["routing_decisions"]


@pytest.mark.asyncio
async def test_non_decision_point_node_never_writes_routing_decisions() -> None:
    """AC4 — a node with only unconditional outgoing edges keeps the exact
    Story 4.2 `functools.partial` wiring, untouched by this story."""
    dag = _dag(["a", "b"], [("a", "b", None)])
    templates = {n: _template() for n in ("a", "b")}
    router = LLMRouter(
        providers={
            "mock": MockProvider("mock", [_completion('{"x": 1}'), _completion('{"y": 2}')])
        },
        default_chain=["mock"],
    )
    graph = build_state_graph(dag, templates, router).compile()

    result = await graph.ainvoke(_initial_state())

    assert not result.get("routing_decisions")


@pytest.mark.asyncio
async def test_two_decision_point_nodes_each_execute_with_their_own_template() -> None:
    """T6.2bis — a closure capturing the LOOP VARIABLE (instead of
    `_make_node_callable`'s own bound parameters) would make every
    decision-point node execute the LAST node's template, with a mono-node
    test suite staying green throughout. Two independent decision-point
    roots with distinct `llm_model`s catch that regression."""
    dag = _dag(
        ["a", "b", "z"],
        [
            ("a", "z", "output.status == 'ok'"),
            ("b", "z", "output.status == 'ok'"),
        ],
    )
    templates = {
        "a": _template_with({"llm_model": "model-a"}),
        "b": _template_with({"llm_model": "model-b"}),
        "z": _template(),
    }
    provider = MockProvider("mock", [], infinite_default=_completion('{"status": "ok"}'))
    router = LLMRouter(providers={"mock": provider}, default_chain=["mock"])
    graph = build_state_graph(
        dag, templates, router, rules=(), routing_settings=_routing_settings()
    ).compile()

    await graph.ainvoke(_initial_state())

    models_used = {call["model"] for call in provider.calls}
    assert {"model-a", "model-b"} <= models_used


def test_make_router_reads_decision_from_state() -> None:
    """The decision in the state WINS over re-resolving the DSL: `c` is
    declared, and it is chosen even though `b`'s condition holds."""
    from agentive_backend.features.workflow_engine.engine.graph_builder import _make_router

    router_fn = _make_router("a", [("b", parse("output.status == 'ok'"))], ["c"])
    state: dict[str, Any] = {
        "routing_decisions": {"a": {"targets": ["c"]}},
        "node_outputs": {"a": {"status": "ok"}},
    }
    assert router_fn(state) == ["c"]


def test_make_router_when_checkpointed_target_is_not_declared_should_end() -> None:
    """A decision read back from checkpointed JSONB is untrusted input: it
    may name an edge a later `PATCH` removed, or have been hand-edited during
    an incident. A stale id must degrade to END — the same terminal outcome
    as "no condition held" — not reach LangGraph and raise
    `InvalidUpdateError` mid-resume."""
    from langgraph.graph import END

    from agentive_backend.features.workflow_engine.engine.graph_builder import _make_router

    router_fn = _make_router("a", [("b", parse("output.status == 'ok'"))], [])
    state: dict[str, Any] = {
        "routing_decisions": {"a": {"targets": ["removed-by-a-patch"]}},
        "node_outputs": {"a": {"status": "ok"}},
    }
    assert router_fn(state) == [END]


def test_make_router_when_checkpointed_targets_is_a_bare_string_should_not_explode() -> None:
    """`list("b")` is `["b"]` only by accident; `list("bc")` is `["b", "c"]`.
    A `targets` that deserialized as a string must not be walked character by
    character into node ids."""
    from agentive_backend.features.workflow_engine.engine.graph_builder import _make_router

    router_fn = _make_router("a", [("bc", parse("output.status == 'ok'"))], [])
    state: dict[str, Any] = {
        "routing_decisions": {"a": {"targets": "bc"}},
        "node_outputs": {"a": {"status": "ok"}},
    }
    assert router_fn(state) == ["bc"]


def test_make_router_returns_end_when_decision_targets_empty() -> None:
    from langgraph.graph import END

    from agentive_backend.features.workflow_engine.engine.graph_builder import _make_router

    router_fn = _make_router("a", [("b", parse("output.status == 'ok'"))], [])
    state: dict[str, Any] = {"routing_decisions": {"a": {"targets": []}}, "node_outputs": {}}
    assert router_fn(state) == [END]


def test_make_router_falls_back_to_pure_resolution_when_routing_decisions_absent() -> None:
    """AC4 — resuming a run checkpointed BEFORE this story shipped: the
    restored LangGraph state has no `routing_decisions` key at all."""
    from agentive_backend.features.workflow_engine.engine.graph_builder import _make_router

    router_fn = _make_router("a", [("b", parse("output.status == 'ok'"))], ["c"])
    state: dict[str, Any] = {"node_outputs": {"a": {"status": "ok"}}}  # no "routing_decisions"
    assert set(router_fn(state)) == {"b", "c"}


def test_build_state_graph_raises_if_decision_point_has_no_routing_settings() -> None:
    dag = _dag(["a", "b"], [("a", "b", "output.status == 'ok'")])
    templates = {n: _template() for n in ("a", "b")}
    router = LLMRouter(
        providers={"mock": MockProvider("mock", [], infinite_default=_completion("{}"))},
        default_chain=["mock"],
    )
    with pytest.raises(ValueError, match="routing_settings"):
        build_state_graph(dag, templates, router)


# ─── IG3 — a failed escalation must not discard the node's paid work ──────


@pytest.mark.asyncio
async def test_failed_escalation_preserves_the_node_update_on_the_exception() -> None:
    """The node ALREADY ran: its LLM call is paid, its tokens are spent, its
    output exists. LangGraph discards the state update of a node that raises,
    so that work used to vanish — a run ended `error` with no trace of a node
    that did execute, and its cost missing from the run's totals.

    The run must still fail (explicit-failure posture unchanged); the update
    simply has to travel WITH the exception so the service can commit it.
    """
    from agentive_backend.features.workflow_engine.domain.routing_rules import (
        RoutingEscalationError,
    )
    from agentive_backend.features.workflow_engine.engine.graph_builder import (
        RoutingDecisionFailedError,
        _make_node_callable,
    )
    from agentive_backend.features.workflow_engine.engine.hybrid_router import RoutingSettings

    node_update = {
        "node_outputs": {"a": {"status": "unknown"}},
        "node_metrics": {"a": {"input_tokens": 11, "output_tokens": 3, "cost_usd": "0.002"}},
    }

    async def _fake_execute(_state: Any, **_kwargs: Any) -> dict[str, Any]:
        return node_update

    monkey = pytest.MonkeyPatch()
    try:
        import agentive_backend.features.workflow_engine.engine.graph_builder as gb

        monkey.setattr(gb, "execute_agent_node", _fake_execute)

        llm_router = AsyncMock()
        llm_router.complete = AsyncMock(return_value=_completion("not json at all"))

        callable_ = _make_node_callable(
            node_id="a",
            template=_template(),
            llm_router=llm_router,
            conditional_edges=[("b", parse("output.status == 'never'"))],
            unconditional_targets=[],
            rules=[],
            routing_settings=RoutingSettings(
                threshold=0.8,
                escalation_model="claude-haiku-4-5",
                escalation_timeout_s=15.0,
                escalation_max_tokens=256,
            ),
        )

        with pytest.raises(RoutingDecisionFailedError) as exc_info:
            await callable_({"task_input": {}})  # type: ignore[arg-type]
    finally:
        monkey.undo()

    # The run still fails, and for the REAL reason — the wrapper is only a
    # carrier, the cause stays reachable for `checkpoint["last_error"]`.
    assert isinstance(exc_info.value.__cause__, RoutingEscalationError)
    assert exc_info.value.node_id == "a"
    # And the work the run already paid for came along.
    assert exc_info.value.node_update["node_outputs"] == {"a": {"status": "unknown"}}
    assert exc_info.value.node_update["node_metrics"]["a"]["cost_usd"] == "0.002"
