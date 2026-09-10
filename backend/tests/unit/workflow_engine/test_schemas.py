"""Unit tests — Workflow Engine Pydantic schemas (Story 4.1 code-review patch).

Covers the schema-level defensive validations added on review: whitespace-only
`name` rejection, and size caps on `nodes`/`edges`/edge node-id strings.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentive_backend.features.workflow_engine.schemas import (
    _MAX_EDGES,
    _MAX_NODES,
    CreateWorkflowRequest,
    WorkflowEdgeRequest,
    WorkflowNodeRequest,
)


def _node(node_id: str = "a") -> WorkflowNodeRequest:
    return WorkflowNodeRequest(node_id=node_id, agent_template_id=uuid4())


def test_name_whitespace_only_is_rejected() -> None:
    with pytest.raises(ValidationError, match="blank or whitespace-only"):
        CreateWorkflowRequest(name="   ", nodes=[_node()], edges=[])


def test_name_is_stripped() -> None:
    request = CreateWorkflowRequest(name="  my-workflow  ", nodes=[_node()], edges=[])
    assert request.name == "my-workflow"


def test_nodes_over_max_length_is_rejected() -> None:
    nodes = [_node(node_id=f"n{i}") for i in range(_MAX_NODES + 1)]
    with pytest.raises(ValidationError):
        CreateWorkflowRequest(name="too-many-nodes", nodes=nodes, edges=[])


def test_edges_over_max_length_is_rejected() -> None:
    nodes = [_node("a"), _node("b")]
    edges = [WorkflowEdgeRequest(from_node_id="a", to_node_id="b") for _ in range(_MAX_EDGES + 1)]
    with pytest.raises(ValidationError):
        CreateWorkflowRequest(name="too-many-edges", nodes=nodes, edges=edges)


def test_edge_from_node_id_over_max_length_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowEdgeRequest(from_node_id="x" * 101, to_node_id="b")


def test_edge_to_node_id_over_max_length_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowEdgeRequest(from_node_id="a", to_node_id="x" * 101)


def test_edge_node_ids_at_max_length_are_accepted() -> None:
    edge = WorkflowEdgeRequest(from_node_id="x" * 100, to_node_id="y" * 100)
    assert len(edge.from_node_id) == 100
    assert len(edge.to_node_id) == 100


# ─── Lot 3 — finding #24 : node ids LangGraph reserves ─────────────────


@pytest.mark.parametrize("reserved", ["__start__", "__end__", "__interrupt__", "__x__"])
def test_reserved_node_id_is_refused_at_creation(reserved: str) -> None:
    """LangGraph's `StateGraph.add_node` REFUSES these names. Accepted at
    creation (4.1 only bounded the length), such a workflow blew up at
    `build_state_graph` time instead — i.e. at RUN time, on a background
    task, for every run of that workflow forever."""
    with pytest.raises(ValidationError, match="reserved"):
        WorkflowNodeRequest(node_id=reserved, agent_template_id=uuid4())


@pytest.mark.parametrize("field", ["from_node_id", "to_node_id"])
def test_reserved_edge_endpoint_is_refused(field: str) -> None:
    """Otherwise these surfaced as the much vaguer "edge references
    undeclared node"."""
    kwargs = {"from_node_id": "a", "to_node_id": "b", field: "__end__"}
    with pytest.raises(ValidationError, match="reserved"):
        WorkflowEdgeRequest(**kwargs)


@pytest.mark.parametrize("ordinary", ["__init", "start__", "_private", "a__b", "__"])
def test_ordinary_node_ids_with_underscores_are_still_accepted(ordinary: str) -> None:
    """The guard targets dunder-WRAPPED names only — it must not become a
    blanket ban on underscores."""
    assert WorkflowNodeRequest(node_id=ordinary, agent_template_id=uuid4()).node_id == ordinary
