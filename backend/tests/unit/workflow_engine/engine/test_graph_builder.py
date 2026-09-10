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
from uuid import uuid4

import pytest

from agentive_backend.features.workflow_engine.domain.value_objects import (
    WorkflowDag,
    WorkflowEdge,
    WorkflowNode,
)
from agentive_backend.features.workflow_engine.engine.graph_builder import build_state_graph
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


def _dag(nodes: list[str], edges: list[tuple[str, str, str | None]]) -> WorkflowDag:
    return WorkflowDag(
        nodes=tuple(WorkflowNode(node_id=n, agent_template_id=uuid4()) for n in nodes),
        edges=tuple(WorkflowEdge(from_node_id=f, to_node_id=t, condition=c) for f, t, c in edges),
    )


def _initial_state() -> dict[str, Any]:
    return {"task_input": {}, "correlation_id": "cid", "node_outputs": {}, "node_metrics": {}}


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
    graph = build_state_graph(dag, templates, router).compile()

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
    graph = build_state_graph(dag, templates, router).compile()

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
