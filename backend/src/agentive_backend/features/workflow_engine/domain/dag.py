"""DAG structural validations (Story 4.1 AC1) — pure functions, no I/O.

``detect_cycle`` uses :class:`graphlib.TopologicalSorter` (stdlib, Python
3.14 — already the interpreter target of this repo). Zero new dependency:
``networkx``/``rustworkx`` are deliberately NOT added for this need.
"""

from __future__ import annotations

import graphlib

from agentive_backend.features.workflow_engine.domain.value_objects import WorkflowDag, WorkflowEdge


def find_duplicate_node_ids(dag: WorkflowDag) -> list[str]:
    """Return the ``node_id`` values that appear more than once (empty if none)."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for node in dag.nodes:
        if node.node_id in seen:
            if node.node_id not in duplicates:
                duplicates.append(node.node_id)
        else:
            seen.add(node.node_id)
    return duplicates


def find_dangling_edges(dag: WorkflowDag) -> list[WorkflowEdge]:
    """Return edges whose ``from_node_id``/``to_node_id`` is not a declared node."""
    node_ids = {node.node_id for node in dag.nodes}
    return [
        edge
        for edge in dag.edges
        if edge.from_node_id not in node_ids or edge.to_node_id not in node_ids
    ]


def detect_cycle(dag: WorkflowDag) -> list[str] | None:
    """Return the ``node_id`` list involved in a cycle, or ``None`` if acyclic.

    Nodes without any edge (isolated) are included in the graph as valid,
    dependency-free entries — a single-node DAG with no edges is not a cycle.
    Assumes :func:`find_dangling_edges` has already been checked (edges
    referencing an undeclared node are silently ignored here).
    """
    node_ids = {node.node_id for node in dag.nodes}
    graph: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for edge in dag.edges:
        if edge.from_node_id in node_ids and edge.to_node_id in node_ids:
            graph[edge.to_node_id].add(edge.from_node_id)

    sorter: graphlib.TopologicalSorter[str] = graphlib.TopologicalSorter(graph)
    try:
        list(sorter.static_order())
    except graphlib.CycleError as exc:
        cycle_nodes = exc.args[1]
        deduped: list[str] = []
        for node_id in cycle_nodes:
            if node_id not in deduped:
                deduped.append(node_id)
        return deduped
    return None


__all__ = ["detect_cycle", "find_dangling_edges", "find_duplicate_node_ids"]
