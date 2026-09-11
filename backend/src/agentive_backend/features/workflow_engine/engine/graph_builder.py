"""Compile a persisted DAG (Story 4.1) into a LangGraph ``StateGraph`` (Story 4.2 T3.2).

Reimplements the patterns validated by the M3 spike
(``backend/spike/m3_langgraph.py``, ``docs/decisions/m3-spike-result.md``) in
production code — the spike itself is never imported (deliberately isolated,
not a reusable package).

Story 4.3 (T4, T6) layers hybrid routing (DSL → declarative rules → LLM
escalation) on top of the structure built here, without touching a node
whose outgoing edges are all unconditional (AC4 — those keep the exact
``functools.partial(execute_agent_node, ...)`` wiring from Story 4.2). See
``engine/hybrid_router.py`` for the decision itself.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from agentive_backend.features.workflow_engine.domain.condition_dsl import (
    ParsedCondition,
    evaluate,
    parse,
)
from agentive_backend.features.workflow_engine.domain.routing_rules import RoutingEscalationError
from agentive_backend.features.workflow_engine.domain.value_objects import WorkflowState
from agentive_backend.features.workflow_engine.engine.agent_node import execute_agent_node
from agentive_backend.shared.llm.exceptions import LLMError

if TYPE_CHECKING:
    from agentive_backend.features.workflow_engine.domain.routing_rules import RoutingRule
    from agentive_backend.features.workflow_engine.domain.value_objects import WorkflowDag
    from agentive_backend.features.workflow_engine.engine.hybrid_router import RoutingSettings
    from agentive_backend.infra.db.models import AgentTemplate
    from agentive_backend.shared.llm.router import LLMRouter


class RoutingDecisionFailedError(RuntimeError):
    """A node executed successfully but its routing decision could not be made.

    Carries the node's own state update (``node_outputs``/``node_metrics``)
    so the service can persist the work the run already paid for before
    marking it ``error`` — LangGraph discards the update of a node that
    raises, which used to make that spend vanish without trace (review IG3).

    The original failure is always reachable through ``__cause__``; the
    service reads it back so ``checkpoint["last_error"]`` still describes the
    real cause and not this wrapper.
    """

    def __init__(self, *, node_id: str, node_update: dict[str, Any]) -> None:
        super().__init__(f"routing decision failed for node {node_id!r}")
        self.node_id = node_id
        self.node_update = node_update


def resolve_deterministic_targets(
    own_output: Any,
    conditional_edges: Sequence[tuple[str, ParsedCondition]],
    unconditional_targets: Sequence[str],
) -> list[str]:
    """Pure resolution of the 4.1/4.2 branching-condition DSL (Story 4.3 T4.1).

    Extracted from the body of Story 4.2's ``_router`` closure so it has a
    SINGLE implementation with two callers: :func:`_make_router`'s fallback
    for a run checkpointed before this story (T6.3, AC4), and
    ``engine/hybrid_router.decide_route`` step (a) (T5.2).

    Returns the union of every unconditional target PLUS every conditional
    target whose condition evaluates ``True`` against ``own_output`` (never
    raises — T3.1's ``evaluate`` degrades a missing/mismatched variable to
    ``False``). Deliberately does NOT fall back to ``[END]`` when empty —
    that terminal decision belongs to the caller (Story 4.2 ``_router``
    decided it inline; Story 4.3 callers decide it differently depending on
    whether rules/LLM still get a say).
    """
    targets = list(unconditional_targets)
    for to_node_id, parsed in conditional_edges:
        actual = own_output.get(parsed.field) if isinstance(own_output, dict) else None
        if evaluate(parsed, actual):
            targets.append(to_node_id)
    return targets


def _make_router(
    from_node_id: str,
    conditional_edges: list[tuple[str, ParsedCondition]],
    unconditional_targets: list[str],
) -> Callable[[WorkflowState], list[str]]:
    """Build the ``add_conditional_edges`` router closure for one source node.

    Conditions are pre-parsed once at graph-build time (not per invocation) —
    they were already syntax-validated at workflow creation (Story 4.1 AC1),
    so re-parsing per call would only pay the cost again for no benefit.

    Story 4.3 T6.3 — this is now purely a READER. The decision itself was
    already computed and stored in ``state["routing_decisions"]`` by the
    EMITTING node's own step (:func:`_make_node_callable`, T6.2) — LangGraph
    guarantees that a node's state update lands BEFORE its outgoing router is
    invoked (the Pregel superstep model; verified empirically in the Story
    4.2 review against ``predecessor_outputs``), so the decision is always
    present for a node that went through :func:`_make_node_callable`.

    The fallback to :func:`resolve_deterministic_targets` below is NOT dead
    code even though every 4.3 decision point always populates
    ``routing_decisions``: it is the path taken when resuming a run
    checkpointed BEFORE this story shipped, whose LangGraph state has no
    ``routing_decisions`` key at all (AC4 non-regression).
    """

    # `END` belongs in the allowed set: a `terminate` verdict and an LLM that
    # answered END both record it as a REAL target (Story 4.3 T2.3/T5.2), so
    # that a deliberate termination stays distinguishable from "no targets"
    # in the checkpoint and in the escalation event.
    declared_successors = frozenset(
        [to_node_id for to_node_id, _ in conditional_edges] + list(unconditional_targets) + [END]
    )

    def _router(state: WorkflowState) -> list[str]:
        routing_decisions = state.get("routing_decisions") or {}
        decision = routing_decisions.get(from_node_id)
        if decision is not None:
            # The decision is READ BACK from checkpointed JSONB, so it is
            # untrusted input by the time it gets here: it may predate a
            # `PATCH` that removed an edge, or have been hand-edited during
            # an incident. Keeping only declared successors means a stale id
            # degrades to `[END]` — the same terminal outcome as "no
            # condition held" — instead of reaching LangGraph and raising
            # `InvalidUpdateError` mid-resume. The `isinstance` filter is not
            # decorative either: a `targets` that deserialized as a bare
            # string would otherwise be exploded character by character by
            # `list()`.
            raw_targets = decision.get("targets") or []
            if isinstance(raw_targets, str):
                raw_targets = [raw_targets]
            targets = [t for t in raw_targets if isinstance(t, str) and t in declared_successors]
            return targets if targets else [END]

        node_outputs = state.get("node_outputs") or {}
        own_output = node_outputs.get(from_node_id)
        targets = resolve_deterministic_targets(
            own_output, conditional_edges, unconditional_targets
        )
        return targets if targets else [END]

    return _router


def _make_node_callable(
    *,
    node_id: str,
    template: AgentTemplate,
    llm_router: LLMRouter,
    conditional_edges: list[tuple[str, ParsedCondition]],
    unconditional_targets: list[str],
    rules: Sequence[RoutingRule],
    routing_settings: RoutingSettings,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Factory for a routing-DECISION-POINT node's callable (Story 4.3 T6.2).

    A MODULE-LEVEL factory, not a closure built inline inside the
    ``for node in dag.nodes`` loop — every parameter here is bound at CALL
    time to this factory, not captured as a loop variable. A closure
    defined directly inside the loop would instead capture the loop
    *variable*, and every node would end up executing whichever template
    the LAST loop iteration left behind — with a mono-node-per-test suite
    staying perfectly green throughout (T6.2bis). ``functools.partial`` in
    :func:`build_state_graph` already gets this right for non-decision
    nodes; this factory gives decision-point nodes the same guarantee.

    Local import of ``hybrid_router.decide_route`` (not top-of-module) to
    avoid a circular import: ``hybrid_router`` imports
    :func:`resolve_deterministic_targets` from THIS module at its own
    top level, so this module cannot import ``hybrid_router`` at import
    time without a cycle. Safe here because this factory only runs at
    graph-BUILD time, well after both modules have finished loading.
    """
    from agentive_backend.features.workflow_engine.engine.hybrid_router import decide_route

    async def _call(state: WorkflowState) -> dict[str, Any]:
        node_update = await execute_agent_node(
            state, template=template, llm_router=llm_router, node_id=node_id
        )
        own_output = (node_update.get("node_outputs") or {}).get(node_id)
        try:
            decision = await decide_route(
                node_id=node_id,
                own_output=own_output,
                conditional_edges=conditional_edges,
                unconditional_targets=unconditional_targets,
                rules=rules,
                routing_settings=routing_settings,
                llm_router=llm_router,
                task_input=state.get("task_input"),
            )
        except (RoutingEscalationError, LLMError) as exc:
            # The node ALREADY ran: its LLM call is paid, its tokens are
            # spent, its output exists. Raising from here discards the whole
            # state update with the exception, so that work vanished — a run
            # would end `error` with no trace of a node that did execute, and
            # its cost missing from the run's totals (review IG3).
            #
            # The explicit-failure posture is unchanged: this re-raises, the
            # run still ends `error`. The update merely travels WITH the
            # exception so the service can commit it to the checkpoint before
            # marking the run. Only these two types are caught — a
            # `CancelledError` (a `BaseException`) still passes straight
            # through, as trap #4 requires.
            raise RoutingDecisionFailedError(node_id=node_id, node_update=node_update) from exc
        node_update["routing_decisions"] = {node_id: decision.to_mapping()}
        return node_update

    return _call


def build_state_graph(
    dag: WorkflowDag,
    templates: dict[str, AgentTemplate],
    llm_router: LLMRouter,
    *,
    rules: Sequence[RoutingRule] = (),
    routing_settings: RoutingSettings | None = None,
) -> StateGraph[WorkflowState, None, WorkflowState, WorkflowState]:
    """Build (uncompiled) the ``StateGraph`` for ``dag`` — caller ``.compile()``s
    it with a checkpointer (T5.3).

    * Edges are grouped by ``from_node_id`` FIRST (Story 4.3 T6.1 — needed
      before ``add_node`` so each node knows, at add-time, whether it is a
      routing DECISION POINT: a node with at least one conditional outgoing
      edge, exactly the set for which ``add_conditional_edges`` is installed
      below). A decision-point node is bound via
      :func:`_make_node_callable` (T6.2, composes execution + the hybrid
      decision); every other node keeps the Story 4.2
      ``functools.partial(execute_agent_node, ...)`` wiring UNCHANGED (AC4).
    * A source with at least one conditional edge routes ALL its outgoing
      edges (conditional AND unconditional) through a single
      :func:`_make_router` — LangGraph allows only one outgoing-edge
      mechanism per source node, so mixing ``add_edge`` and
      ``add_conditional_edges`` for the same source is not possible. A
      source with only unconditional edges uses plain ``add_edge`` calls
      instead.
    * Root nodes (no incoming edge) get ``add_edge(START, root_id)`` —
      supports multiple parallel entry points (fan-out) natively.
    * Sink nodes (no outgoing edge at all) get ``add_edge(sink_id, END)``.
      A node whose ALL outgoing edges are conditional but structurally
      declared is NOT a sink here — its runtime "no condition held" case is
      handled dynamically by the router returning ``[END]``, not a static edge.

    ``rules``/``routing_settings`` default to an empty catalog / ``None`` so
    callers that only need the Story 4.2 shape (e.g. the ``test_graph_builder``
    suite predating this story) keep compiling — but any node that IS a
    decision point requires a real ``routing_settings`` (``None`` there is a
    caller bug, not a degrade-quietly case).
    """
    builder: StateGraph[WorkflowState, None, WorkflowState, WorkflowState] = StateGraph(
        WorkflowState
    )

    edges_by_source: dict[str, list[tuple[str, str | None]]] = {}
    for edge in dag.edges:
        edges_by_source.setdefault(edge.from_node_id, []).append((edge.to_node_id, edge.condition))

    conditional_by_source: dict[str, list[tuple[str, ParsedCondition]]] = {}
    unconditional_by_source: dict[str, list[str]] = {}
    for from_node_id, targets in edges_by_source.items():
        conditional_by_source[from_node_id] = [
            (to_node_id, parse(condition))
            for to_node_id, condition in targets
            if condition is not None
        ]
        unconditional_by_source[from_node_id] = [
            to_node_id for to_node_id, condition in targets if condition is None
        ]

    for node in dag.nodes:
        conditional_edges = conditional_by_source.get(node.node_id, [])
        if conditional_edges:
            if routing_settings is None:
                raise ValueError(
                    f"node '{node.node_id}' has conditional outgoing edges but no "
                    "routing_settings was provided to build_state_graph"
                )
            node_callable: Callable[[WorkflowState], Any] = _make_node_callable(
                node_id=node.node_id,
                template=templates[node.node_id],
                llm_router=llm_router,
                conditional_edges=conditional_edges,
                unconditional_targets=unconditional_by_source.get(node.node_id, []),
                rules=rules,
                routing_settings=routing_settings,
            )
        else:
            node_callable = functools.partial(
                execute_agent_node,
                template=templates[node.node_id],
                llm_router=llm_router,
                node_id=node.node_id,
            )
        # LangGraph's `add_node` overloads resolve against the CONCRETE callable
        # expression at the call site (a bare `functools.partial(...)`); routed
        # through a `node_callable` variable typed broadly enough to hold BOTH
        # branches, mypy can no longer pick an overload. Runtime behavior is
        # unaffected — both branches are `Callable[[WorkflowState], Awaitable[dict]]`.
        builder.add_node(node.node_id, node_callable)  # type: ignore[call-overload]

    for from_node_id in edges_by_source:
        conditional_edges = conditional_by_source[from_node_id]
        unconditional_targets = unconditional_by_source[from_node_id]
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


__all__ = ["build_state_graph", "resolve_deterministic_targets"]
