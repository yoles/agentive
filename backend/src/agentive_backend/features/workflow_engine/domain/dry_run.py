"""Dry Run predictive estimation — pure domain core (Story 4.4 AC1, T2).

Framework-free (dataclasses/TypedDict only, no Pydantic, no I/O) — mirrors
the posture of :mod:`.dag`/:mod:`.routing_rules`. ``.import-linter``
Contract 2/3 compliance: no ``infra.*``/``sqlalchemy``/``pydantic`` imports
here.

``resolve_deterministic_targets``/``decide_route`` (Story 4.3, ``engine/``)
cannot be reused here: both require a node's actual ``own_output``, which
only exists AFTER that node executes. This module never fabricates one —
see Story 4.4 Dev Notes § Algorithme ``probable_path`` for the full
rationale. Instead it predicts a path from two sources only: the DAG's own
structure (edges with no branching condition are 100% certain, exactly
Story 4.3's own definition of "not a decision point",
``engine/graph_builder.py:280``) and history (the majority target this
workflow's past runs actually took at each real decision point).

Not imported from ``engine/graph_builder.py`` even though the logic mirrors
it (``find_root_nodes``, decision-point classification): ``domain/`` stays
framework-free, ``engine/`` is the layer above it, importing it here would
invert that dependency direction. The small duplication is deliberate.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, TypedDict

from agentive_backend.features.workflow_engine.domain.value_objects import WorkflowDag

#: Mirrors ``langgraph.graph.END``'s literal value (``"__end__"``) WITHOUT
#: importing langgraph — this module stays framework-free. Safe to hardcode:
#: node ids wrapped in double underscores are already rejected at workflow
#: creation (``schemas._RESERVED_NODE_ID_RE``), so no real DAG node can ever
#: collide with this sentinel. A parity test against the real ``END``
#: constant lives in the test suite (not here — that import belongs to a
#: test file, not to production domain code).
END_SENTINEL: Final = "__end__"

#: `history[node_id][targets]` = how many past runs of this workflow chose
#: that exact tuple of targets at this decision point. Built by the caller
#: (`DryRunService`, T3 — has DB access) from
#: `WorkflowRun.checkpoint["routing_decisions"]` across past runs. Never
#: built inside this module, which stays pure.
RoutingHistory = Mapping[str, Mapping[tuple[str, ...], int]]


class NodeMetricSample(TypedDict, total=False):
    """One historical execution sample for a single node — the token
    fields consumed from ``workflow_runs.metrics.per_node[node_id]``
    (``features/workflow_engine/service.py:562-568``, ``_aggregate_metrics``).

    Deliberately does NOT carry that shape's ``cost_usd``/``model_used``:
    Dry Run always recomputes cost from (possibly averaged) token counts
    against the CURRENT pricing table (``dry_run.py``'s
    ``_resolve_provider_and_cost``), never by averaging historical dollar
    amounts — a model's price can change between the sampled runs and now,
    and this module has no use for a stale average.
    """

    input_tokens: int | None
    output_tokens: int | None


def find_root_nodes(dag: WorkflowDag) -> tuple[str, ...]:
    """Nodes with no incoming edge, in DAG declaration order, deduplicated.

    Pure reimplementation of the same structural fact
    ``build_state_graph`` uses to wire LangGraph's ``START``
    (``engine/graph_builder.py:320-324``).

    ``dict.fromkeys`` rather than a plain generator: duplicate node ids are
    rejected at creation (Story 4.1 ``find_duplicate_node_ids``), but a
    stored DAG carrying the same root twice used to enter the reachability
    queue twice and have its tokens AND cost counted twice in the estimate
    — a silent doubling, on the one number this endpoint exists to produce
    (review fix P7).
    """
    to_node_ids = {edge.to_node_id for edge in dag.edges}
    return tuple(
        dict.fromkeys(node.node_id for node in dag.nodes if node.node_id not in to_node_ids)
    )


def classify_decision_points(dag: WorkflowDag) -> frozenset[str]:
    """Node ids with >=1 outgoing edge that carries a branching condition.

    Exactly Story 4.3's definition of a routing decision point
    (``engine/graph_builder.py:280``, ``if conditional_edges:``) — a node
    whose every outgoing edge is unconditional is structurally determined,
    never a decision. Only needs ``WorkflowEdge.condition``'s
    presence/absence: Dry Run can never evaluate a condition's content
    without ``own_output`` anyway (module docstring).
    """
    return frozenset(edge.from_node_id for edge in dag.edges if edge.condition is not None)


def _targets_by_source(dag: WorkflowDag) -> dict[str, tuple[str, ...]]:
    """Every outgoing target per source node, declaration order,
    deduplicated, and restricted to targets that actually exist as nodes.

    Mirrors the ``dict.fromkeys`` ordering precedent of
    ``engine/hybrid_router.py:225-229`` — duplicates must go, declaration
    order must stay (it is what the deterministic tie-break below iterates
    over). Conditional AND unconditional edges are combined here: at a
    decision point, every declared target is a candidate the real decision
    could have picked (mirrors ``declared_candidates`` in
    ``engine/hybrid_router.py``).

    Dangling targets are dropped HERE rather than at each use site
    (review fix P6). Dangling edges are rejected at creation (Story 4.1
    ``find_dangling_edges``), so this is defensive — but the guard used to
    exist only in the reachability walk, not on the ``probable_path`` walk,
    so a corrupted DAG produced a ``probable_path`` containing a node id
    absent from both ``agents_involved`` and the DAG itself. Every consumer
    of that contract (``on_probable_path``, Epic 6's Dialog) would have
    keyed on a node that does not exist. Filtering once keeps the two walks
    structurally unable to disagree.
    """
    node_ids = {node.node_id for node in dag.nodes}
    grouped: dict[str, list[str]] = {}
    for edge in dag.edges:
        if edge.to_node_id not in node_ids:
            continue
        grouped.setdefault(edge.from_node_id, []).append(edge.to_node_id)
    return {source: tuple(dict.fromkeys(targets)) for source, targets in grouped.items()}


@dataclass(frozen=True, slots=True)
class ProbablePathResult:
    """Output of :func:`compute_probable_path`."""

    #: A single representative sequence of node ids, root to sink (or to a
    #: recorded ``END`` decision). Ties/fan-outs/uncertain decision points
    #: are resolved deterministically (first declared target) — see module
    #: docstring and Story 4.4 Dev Notes § Algorithme ``probable_path``.
    probable_path: tuple[str, ...]
    #: EVERY node structurally reachable from any root, following ALL edges
    #: regardless of condition — a strict superset of `probable_path`. A
    #: node that might run must be priced even when it is not on the single
    #: representative path (excluding it would understate cost, the
    #: opposite of a Dry Run's purpose).
    agents_involved: tuple[str, ...]
    #: Decision-point node ids with no historical majority (no history, a
    #: tie, or a majority gone stale) — surfaced by the caller as an
    #: `identified_risks` entry (`routing_decision_uncertain`).
    #:
    #: Covers EVERY reachable decision point, not only those the
    #: representative path happens to cross (Story 4.4 review, IG3). The two
    #: sets used to be the same because uncertainty was collected during the
    #: path walk — so a workflow whose uncertainty sat on a branch the path
    #: did not take was reported as certain, while `agents_involved` billed
    #: that branch anyway. Ordered by `agents_involved` (BFS), deterministic.
    uncertain_decision_points: tuple[str, ...]
    #: Decision points whose winning historical decision named MORE THAN ONE
    #: target — a conditional fan-out the engine really would run in
    #: parallel (Story 4.4 review, IG4). `probable_path` can only show one
    #: of them, so the caller reports the rest rather than letting the
    #: response imply a single branch. Not the same thing as uncertainty:
    #: the history here is unambiguous, it simply does not fit in a
    #: sequence.
    multi_target_decision_points: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Decision:
    """One decision point's resolved outcome."""

    #: Next node on the representative path — ``None`` terminates it (a
    #: recorded ``END``, or no declared target left to follow).
    next_node_id: str | None
    is_uncertain: bool
    is_multi_target: bool


def _resolve_decision(
    declared_targets: tuple[str, ...], history_for_node: Mapping[tuple[str, ...], int] | None
) -> _Decision:
    """Resolve ONE decision point's next hop from its historical tally.

    ``next_node_id`` is ``None`` when the resolved decision terminates (a
    recorded ``END``, or a recorded decision whose every target has since
    left the DAG). ``is_uncertain`` is ``True`` when there is no history for
    this node, when a count is tied between >=2 distinct target tuples, or
    when the winning entry had to be repaired because the DAG changed under
    it. When no historical target survives at all, the fallback is the first
    declared target, same rule as the unconditional fan-out repli (module
    docstring) — deterministic and testable rather than an arbitrary pick.

    Stale entries are SKIPPED rather than aborting the lookup (review fix
    P8). Given ``{("ghost",): 10, ("c",): 9}`` where ``ghost`` was since
    removed from the DAG, the previous version discarded 9 real
    observations of ``c`` and guessed the first declared branch instead.
    Ranks are walked in order until one resolves; the node is still reported
    uncertain, because a DAG that changed under its own history is exactly
    what John needs told — but the prediction uses the best evidence left.

    Within one entry, EVERY target is filtered, not just the first (Story
    4.4 review, IG4). That mirrors what the real engine does with the same
    stale data: ``_make_router`` keeps the targets still present on the DAG
    and falls back to ``[END]`` when none are
    (``engine/graph_builder.py:136-147``). Checking only ``targets[0]``
    made the Dry Run predict a branch the engine would never take.
    """
    fallback = declared_targets[0] if declared_targets else None
    if not history_for_node:
        return _Decision(fallback, is_uncertain=True, is_multi_target=False)

    ranked = sorted(history_for_node.items(), key=lambda item: item[1], reverse=True)
    repaired = False
    index = 0
    while index < len(ranked):
        count = ranked[index][1]
        at_this_count = [targets for targets, tally in ranked if tally == count]
        if len(at_this_count) > 1:
            # A perfect tie carries no majority to follow, at any rank.
            return _Decision(fallback, is_uncertain=True, is_multi_target=False)

        recorded = at_this_count[0]
        # A recorded END anywhere in the entry terminates, exactly as
        # `_make_router` treats it.
        if END_SENTINEL in recorded:
            return _Decision(None, is_uncertain=repaired, is_multi_target=False)

        usable = tuple(target for target in recorded if target in declared_targets)
        if usable:
            return _Decision(
                usable[0],
                # Dropping some (but not all) targets means the DAG moved
                # under this history — worth flagging even though a target
                # survived.
                is_uncertain=repaired or len(usable) != len(recorded),
                is_multi_target=len(usable) > 1,
            )

        # Every target of this entry is gone (or the entry was empty) —
        # try the next rank down rather than discarding the whole history.
        repaired = True
        index += len(at_this_count)

    return _Decision(fallback, is_uncertain=True, is_multi_target=False)


def compute_probable_path(dag: WorkflowDag, history: RoutingHistory) -> ProbablePathResult:
    """The Dry Run path-prediction algorithm — see Story 4.4 Dev Notes
    § Algorithme ``probable_path`` for the full specification this
    implements:

    1. Multiple roots → `probable_path` follows the first declared root;
       `agents_involved` covers everything reachable from EVERY root.
    2. A node with only unconditional outgoing edges is not a decision —
       structurally certain. A single target is followed; a pure fan-out
       (>=2 unconditional targets) follows the first declared for
       `probable_path`, but every branch still joins `agents_involved`.
    3. A node with >=1 conditional outgoing edge IS a decision point —
       resolved from `history`'s majority target, never from re-evaluating
       a condition (impossible without `own_output`). No/tied history
       degrades to the first declared target and marks the node uncertain.
    4. Anti-loop guard (`visited`) even though the DAG is guaranteed
       acyclic by construction (Story 4.1 AC1) — local defense, never a
       blind trust in an upstream invariant. `visited` is the ONLY bound
       needed: it caps the walk at the DAG's node count by construction. A
       second `len(probable_path) < 100` ceiling used to sit alongside it,
       which could never be the limiting factor for any DAG the guard
       actually protects against — and on a >100-node DAG would have
       truncated the path SILENTLY, returning a partial prediction
       indistinguishable from a complete one. Removed (review fix P9).
    """
    decision_points = classify_decision_points(dag)
    # Already deduplicated and filtered to existing nodes (P6/P7), so both
    # walks below see exactly the same edge set.
    targets_by_source = _targets_by_source(dag)
    roots = find_root_nodes(dag)

    # `agents_involved` — full structural reachability from every root,
    # following every edge unconditionally.
    involved: list[str] = []
    seen_involved: set[str] = set(roots)
    queue: deque[str] = deque(roots)
    while queue:
        current = queue.popleft()
        involved.append(current)
        for target in targets_by_source.get(current, ()):
            if target in seen_involved:
                continue
            seen_involved.add(target)
            queue.append(target)

    # Resolve EVERY reachable decision point up front, not just the ones
    # the representative path crosses (IG3). The path walk below then only
    # reads these — one resolution per node, no chance of the walk and the
    # reported risks disagreeing about the same node.
    decisions: dict[str, _Decision] = {
        node_id: _resolve_decision(targets_by_source.get(node_id, ()), history.get(node_id))
        for node_id in involved
        if node_id in decision_points
    }

    # `probable_path` — a single representative sequence.
    probable_path: list[str] = []
    visited: set[str] = set()
    current_id: str | None = roots[0] if roots else None
    while current_id is not None and current_id not in visited:
        visited.add(current_id)
        probable_path.append(current_id)
        targets = targets_by_source.get(current_id, ())
        if not targets:
            break
        decision = decisions.get(current_id)
        current_id = decision.next_node_id if decision is not None else targets[0]

    # `involved` order (BFS, declaration order on ties) rather than dict
    # insertion order of `history` — deterministic and testable.
    return ProbablePathResult(
        probable_path=tuple(probable_path),
        agents_involved=tuple(involved),
        uncertain_decision_points=tuple(
            node_id
            for node_id in involved
            if node_id in decisions and decisions[node_id].is_uncertain
        ),
        multi_target_decision_points=tuple(
            node_id
            for node_id in involved
            if node_id in decisions and decisions[node_id].is_multi_target
        ),
    )


def average_node_tokens(samples: Sequence[NodeMetricSample]) -> tuple[int | None, int | None]:
    """Average available input/output token samples for one node.

    Each field is averaged and reported INDEPENDENTLY: ``input_tokens`` and
    ``output_tokens`` can be absent from a sample on their own (a run
    captured one and not the other, or a value was corrupt and
    ``_usable_token_count`` dropped it), and one missing field is no reason
    to discard the other's measured data.

    A field with no usable value across every sample comes back ``None``,
    which tells the caller (``DryRunService``) to apply the configured
    heuristic fallback TO THAT FIELD. It must not come back ``0``: the
    previous version returned ``(0, 50)`` here, so a node whose
    ``input_tokens`` were unusable was priced at zero input cost — a
    fabricated number, indistinguishable in the response from a measured
    one, on a feature whose whole purpose is to budget before spending
    (review fix P3).
    """
    input_values: list[int] = []
    output_values: list[int] = []
    for sample in samples:
        input_tokens = sample.get("input_tokens")
        if input_tokens is not None:
            input_values.append(input_tokens)
        output_tokens = sample.get("output_tokens")
        if output_tokens is not None:
            output_values.append(output_tokens)
    avg_input = round(sum(input_values) / len(input_values)) if input_values else None
    avg_output = round(sum(output_values) / len(output_values)) if output_values else None
    return avg_input, avg_output


__all__ = [
    "END_SENTINEL",
    "NodeMetricSample",
    "ProbablePathResult",
    "RoutingHistory",
    "average_node_tokens",
    "classify_decision_points",
    "compute_probable_path",
    "find_root_nodes",
]
