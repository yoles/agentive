"""Compile a persisted DAG (Story 4.1) into a LangGraph ``StateGraph`` (Story 4.2 T3.2).

Reimplements the patterns validated by the M3 spike
(``backend/spike/m3_langgraph.py``, ``docs/decisions/m3-spike-result.md``) in
production code — the spike itself is never imported (deliberately isolated,
not a reusable package).
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING

from langgraph.graph import END, START, StateGraph

from agentive_backend.features.workflow_engine.domain.condition_dsl import (
    ParsedCondition,
    evaluate,
    parse,
)
from agentive_backend.features.workflow_engine.domain.value_objects import WorkflowState
from agentive_backend.features.workflow_engine.engine.agent_node import execute_agent_node

if TYPE_CHECKING:
    from agentive_backend.features.workflow_engine.domain.value_objects import WorkflowDag
    from agentive_backend.infra.db.models import AgentTemplate
    from agentive_backend.shared.llm.router import LLMRouter


def _make_router(
    from_node_id: str,
    conditional_edges: list[tuple[str, ParsedCondition]],
    unconditional_targets: list[str],
) -> Callable[[WorkflowState], list[str]]:
    """Build the ``add_conditional_edges`` router closure for one source node.

    Conditions are pre-parsed once at graph-build time (not per invocation) —
    they were already syntax-validated at workflow creation (Story 4.1 AC1),
    so re-parsing per call would only pay the cost again for no benefit.

    Returns the union of every unconditional target from this node PLUS
    every conditional target whose condition evaluates ``True`` against the
    node's own runtime output (T3.1's ``evaluate``, never raises). An empty
    result routes to ``END`` — that branch terminates without affecting
    parallel branches from other nodes.
    """

    def _router(state: WorkflowState) -> list[str]:
        node_outputs = state.get("node_outputs") or {}
        own_output = node_outputs.get(from_node_id)
        targets = list(unconditional_targets)
        for to_node_id, parsed in conditional_edges:
            actual = own_output.get(parsed.field) if isinstance(own_output, dict) else None
            if evaluate(parsed, actual):
                targets.append(to_node_id)
        return targets if targets else [END]

    return _router


def build_state_graph(
    dag: WorkflowDag,
    templates: dict[str, AgentTemplate],
    llm_router: LLMRouter,
) -> StateGraph[WorkflowState, None, WorkflowState, WorkflowState]:
    """Build (uncompiled) the ``StateGraph`` for ``dag`` — caller ``.compile()``s
    it with a checkpointer (T5.3).

    * One ``add_node`` per DAG node, bound to :func:`execute_agent_node` via
      ``functools.partial`` (``template``/``llm_router``/``node_id`` closed
      over — LangGraph calls the node with only ``state``).
    * Edges are grouped by ``from_node_id``. A source with at least one
      conditional edge routes ALL its outgoing edges (conditional AND
      unconditional) through a single :func:`_make_router` — LangGraph
      allows only one outgoing-edge mechanism per source node, so mixing
      ``add_edge`` and ``add_conditional_edges`` for the same source is not
      possible. A source with only unconditional edges uses plain
      ``add_edge`` calls instead.
    * Root nodes (no incoming edge) get ``add_edge(START, root_id)`` —
      supports multiple parallel entry points (fan-out) natively.
    * Sink nodes (no outgoing edge at all) get ``add_edge(sink_id, END)``.
      A node whose ALL outgoing edges are conditional but structurally
      declared is NOT a sink here — its runtime "no condition held" case is
      handled dynamically by the router returning ``[END]``, not a static edge.
    """
    builder: StateGraph[WorkflowState, None, WorkflowState, WorkflowState] = StateGraph(
        WorkflowState
    )

    for node in dag.nodes:
        builder.add_node(
            node.node_id,
            functools.partial(
                execute_agent_node,
                template=templates[node.node_id],
                llm_router=llm_router,
                node_id=node.node_id,
            ),
        )

    edges_by_source: dict[str, list[tuple[str, str | None]]] = {}
    for edge in dag.edges:
        edges_by_source.setdefault(edge.from_node_id, []).append((edge.to_node_id, edge.condition))

    for from_node_id, targets in edges_by_source.items():
        conditional_edges = [
            (to_node_id, parse(condition))
            for to_node_id, condition in targets
            if condition is not None
        ]
        unconditional_targets = [
            to_node_id for to_node_id, condition in targets if condition is None
        ]
        if conditional_edges:
            builder.add_conditional_edges(
                from_node_id, _make_router(from_node_id, conditional_edges, unconditional_targets)
            )
        else:
            for to_node_id in unconditional_targets:
                builder.add_edge(from_node_id, to_node_id)

    to_node_ids = {edge.to_node_id for edge in dag.edges}
    from_node_ids = {edge.from_node_id for edge in dag.edges}
    for node in dag.nodes:
        if node.node_id not in to_node_ids:
            builder.add_edge(START, node.node_id)
        if node.node_id not in from_node_ids:
            builder.add_edge(node.node_id, END)

    return builder


__all__ = ["build_state_graph"]
