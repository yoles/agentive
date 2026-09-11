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
    DryRunAgentInvolvement,
    DryRunRequest,
    DryRunResponse,
    DryRunRisk,
    ProviderTokenEstimate,
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


# ─── Dry Run schemas (Story 4.4 T4/T6.3) ───────────────────────────────


def test_dry_run_request_defaults_to_empty_input() -> None:
    assert DryRunRequest().input == {}


@pytest.mark.parametrize(
    "model,kwargs",
    [
        (DryRunRequest, {"unexpected": 1}),
        (ProviderTokenEstimate, {"input_tokens": 1, "output_tokens": 1, "unexpected": 1}),
        (
            DryRunAgentInvolvement,
            {
                "node_id": "a",
                "agent_template_id": uuid4(),
                "on_probable_path": True,
                "unexpected": 1,
            },
        ),
        (DryRunRisk, {"code": "no_execution_history", "detail": "x", "unexpected": 1}),
    ],
)
def test_dry_run_schemas_forbid_extra_fields(model: type, kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        model(**kwargs)


def test_provider_token_estimate_rejects_negative_tokens() -> None:
    with pytest.raises(ValidationError):
        ProviderTokenEstimate(input_tokens=-1, output_tokens=0)


def test_dry_run_risk_code_is_a_closed_set() -> None:
    with pytest.raises(ValidationError):
        DryRunRisk(code="recrutement_dynamique", detail="x")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "code",
    [
        "routing_decision_uncertain",
        "routing_decision_multi_target",
        "no_execution_history",
        "node_estimate_from_fallback",
        "model_price_unresolved",
        "budget_cap_exceeded",
    ],
)
def test_dry_run_risk_accepts_every_shipped_code(code: str) -> None:
    """The closed set, enumerated. Three of the six were added by the review
    (IG1/IG2/IG4) so the response could say an estimate is PARTIAL — Story
    9.4 reads these to decide whether a total can be trusted."""
    assert DryRunRisk(code=code, detail="x").code == code  # type: ignore[arg-type]


def test_dry_run_risk_carries_structured_amounts_for_budget_cap() -> None:
    """BS1 — AC3 asks `budget_cap_exceeded` to carry "le montant estimé et
    le seuil dépassé". T4.1's `{code, node_id, detail}` had nowhere to put
    them, so they lived in an English sentence the Epic 6 Dialog would have
    had to parse. `str`, never `Decimal` — the JSON serializer raises on a
    raw `Decimal`, same convention as everywhere else in this response."""
    risk = DryRunRisk(
        code="budget_cap_exceeded",
        detail="Estimated cost 90.000000 USD exceeds the configured cap 50 USD.",
        estimated_usd="90.000000",
        threshold_usd="50",
    )
    assert risk.estimated_usd == "90.000000"
    assert risk.threshold_usd == "50"


def test_dry_run_risk_amounts_default_to_none_on_other_codes() -> None:
    """The two fields are meaningful only for `budget_cap_exceeded` — every
    other code leaves them `None` rather than a fabricated figure."""
    risk = DryRunRisk(code="no_execution_history", detail="never ran")
    assert risk.estimated_usd is None
    assert risk.threshold_usd is None


def test_dry_run_response_round_trips_full_shape() -> None:
    workflow_id = uuid4()
    template_id = uuid4()
    response = DryRunResponse(
        workflow_id=workflow_id,
        probable_path=["a", "b"],
        agents_involved=[
            DryRunAgentInvolvement(
                node_id="a", agent_template_id=template_id, on_probable_path=True
            )
        ],
        token_estimate_per_provider={
            "anthropic": ProviderTokenEstimate(input_tokens=100, output_tokens=50, cost_usd="0.01")
        },
        cost_estimate_usd="0.01",
        probable_path_cost_usd="0.007",
        identified_risks=[DryRunRisk(code="no_execution_history", detail="never ran")],
    )
    assert response.workflow_id == workflow_id
    assert response.probable_path == ["a", "b"]
    assert response.token_estimate_per_provider["anthropic"].cost_usd == "0.01"
    # IG7 — the upper bound and the expected cost are both reported; the
    # expected one is what a `[Lancer]`/`[Annuler]` decision acts on.
    assert response.probable_path_cost_usd == "0.007"


def test_dry_run_response_allows_none_cost_estimate() -> None:
    """No node resolved a price — `None`, never a fabricated `"0"`."""
    response = DryRunResponse(
        workflow_id=uuid4(),
        probable_path=[],
        agents_involved=[],
        token_estimate_per_provider={},
        cost_estimate_usd=None,
        identified_risks=[],
    )
    assert response.cost_estimate_usd is None
    assert response.probable_path_cost_usd is None
