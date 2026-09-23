"""Unit tests — DAG structural validations (Story 4.1 T8.1, AC1)."""

from __future__ import annotations

from uuid import uuid4

from agentive_backend.features.workflow_engine.domain.dag import (
    detect_cycle,
    find_dangling_edges,
    find_duplicate_node_ids,
)
from agentive_backend.features.workflow_engine.domain.value_objects import (
    WorkflowDag,
    WorkflowEdge,
    WorkflowNode,
)


def _node(node_id: str) -> WorkflowNode:
    return WorkflowNode(node_id=node_id, agent_template_id=uuid4())


# ─── find_duplicate_node_ids ──────────────────────────────────────────


def test_find_duplicate_node_ids_returns_empty_for_unique_nodes() -> None:
    dag = WorkflowDag(nodes=(_node("a"), _node("b")), edges=())
    assert find_duplicate_node_ids(dag) == []


def test_find_duplicate_node_ids_finds_single_duplicate() -> None:
    dag = WorkflowDag(nodes=(_node("a"), _node("b"), _node("a")), edges=())
    assert find_duplicate_node_ids(dag) == ["a"]


def test_find_duplicate_node_ids_does_not_repeat_same_id_twice() -> None:
    dag = WorkflowDag(nodes=(_node("a"), _node("a"), _node("a")), edges=())
    assert find_duplicate_node_ids(dag) == ["a"]


# ─── find_dangling_edges ──────────────────────────────────────────────


def test_find_dangling_edges_returns_empty_when_all_edges_valid() -> None:
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b")),
        edges=(WorkflowEdge(from_node_id="a", to_node_id="b"),),
    )
    assert find_dangling_edges(dag) == []


def test_find_dangling_edges_detects_unknown_from_node() -> None:
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b")),
        edges=(WorkflowEdge(from_node_id="ghost", to_node_id="b"),),
    )
    dangling = find_dangling_edges(dag)
    assert len(dangling) == 1
    assert dangling[0].from_node_id == "ghost"


def test_find_dangling_edges_detects_unknown_to_node() -> None:
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b")),
        edges=(WorkflowEdge(from_node_id="a", to_node_id="ghost"),),
    )
    dangling = find_dangling_edges(dag)
    assert len(dangling) == 1
    assert dangling[0].to_node_id == "ghost"


# ─── detect_cycle ─────────────────────────────────────────────────────


def test_detect_cycle_returns_none_for_acyclic_dag() -> None:
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b"), _node("c")),
        edges=(
            WorkflowEdge(from_node_id="a", to_node_id="b"),
            WorkflowEdge(from_node_id="b", to_node_id="c"),
        ),
    )
    assert detect_cycle(dag) is None


def test_detect_cycle_returns_none_for_isolated_nodes() -> None:
    """A DAG with nodes but no edges is valid (mono-agent workflow)."""
    dag = WorkflowDag(nodes=(_node("a"),), edges=())
    assert detect_cycle(dag) is None


def test_detect_cycle_detects_simple_two_node_cycle() -> None:
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b")),
        edges=(
            WorkflowEdge(from_node_id="a", to_node_id="b"),
            WorkflowEdge(from_node_id="b", to_node_id="a"),
        ),
    )
    cycle = detect_cycle(dag)
    assert cycle is not None
    assert set(cycle) == {"a", "b"}


def test_detect_cycle_detects_longer_cycle() -> None:
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b"), _node("c"), _node("d")),
        edges=(
            WorkflowEdge(from_node_id="a", to_node_id="b"),
            WorkflowEdge(from_node_id="b", to_node_id="c"),
            WorkflowEdge(from_node_id="c", to_node_id="d"),
            WorkflowEdge(from_node_id="d", to_node_id="b"),
        ),
    )
    cycle = detect_cycle(dag)
    assert cycle is not None
    assert {"b", "c", "d"}.issubset(set(cycle))
    assert "a" not in cycle


def test_detect_cycle_detects_self_loop() -> None:
    """Smallest possible cycle — a node whose edge points back to itself."""
    dag = WorkflowDag(
        nodes=(_node("a"),),
        edges=(WorkflowEdge(from_node_id="a", to_node_id="a"),),
    )
    cycle = detect_cycle(dag)
    assert cycle is not None
    assert "a" in cycle


def test_detect_cycle_valid_dag_with_branching_is_not_a_cycle() -> None:
    """Diamond shape (a -> b, a -> c, b -> d, c -> d) is a valid DAG."""
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b"), _node("c"), _node("d")),
        edges=(
            WorkflowEdge(from_node_id="a", to_node_id="b"),
            WorkflowEdge(from_node_id="a", to_node_id="c"),
            WorkflowEdge(from_node_id="b", to_node_id="d"),
            WorkflowEdge(from_node_id="c", to_node_id="d"),
        ),
    )
    assert detect_cycle(dag) is None
