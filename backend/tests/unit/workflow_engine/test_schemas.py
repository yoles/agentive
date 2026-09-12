"""Unit tests — Workflow Engine Pydantic schemas (Story 4.1 code-review patch).

Covers the schema-level defensive validations added on review: whitespace-only
`name` rejection, and size caps on `nodes`/`edges`/edge node-id strings.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentive_backend.features.workflow_engine.domain.mise_en_place import CHECK_CODES
from agentive_backend.features.workflow_engine.schemas import (
    _MAX_EDGES,
    _MAX_NODES,
    CreateWorkflowRequest,
    DryRunAgentInvolvement,
    DryRunRequest,
    DryRunResponse,
    DryRunRisk,
    MiseEnPlaceCheckOut,
    MiseEnPlaceReportOut,
    ProviderTokenEstimate,
    StartRunRequest,
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


# ─── StartRunRequest force/reason (Story 4.5 AC3, T5.2) ────────────────


def test_start_run_request_defaults_to_no_bypass() -> None:
    request = StartRunRequest()
    assert request.force is False
    assert request.reason is None


def test_start_run_request_force_without_reason_is_rejected() -> None:
    with pytest.raises(ValidationError, match="reason is required when force=true"):
        StartRunRequest(force=True)


def test_start_run_request_force_with_blank_reason_is_rejected() -> None:
    with pytest.raises(ValidationError, match="reason is required when force=true"):
        StartRunRequest(force=True, reason="   ")


def test_start_run_request_force_with_reason_is_accepted() -> None:
    request = StartRunRequest(force=True, reason="incident P1, deadline serrée")
    assert request.force is True
    assert request.reason == "incident P1, deadline serrée"


def test_start_run_request_reason_without_force_is_rejected() -> None:
    """`reason` alone used to be accepted and then silently discarded —
    never persisted, never published, never echoed back. An operator who
    mistypes the bypass and believes they filed a justification is worse off
    than one who gets a 422 (review P21)."""
    with pytest.raises(ValidationError):
        StartRunRequest(reason="just a note")


def test_start_run_request_blank_reason_without_force_is_accepted() -> None:
    """Only a MEANINGFUL reason conflicts with `force=false`; `None` and
    whitespace stay the ordinary no-bypass request."""
    assert StartRunRequest().reason is None
    assert StartRunRequest(reason="   ").force is False


# ─── Mise en Place schemas (Story 4.5 T5.1, T5.4) ──────────────────────


def _four_checks() -> list[MiseEnPlaceCheckOut]:
    """One entry per check code — the cardinality `MiseEnPlaceReportOut` now
    enforces (review P8)."""
    return [
        MiseEnPlaceCheckOut(code=code, passed=True, detail=f"{code} ok") for code in CHECK_CODES
    ]


@pytest.mark.parametrize(
    "model,kwargs",
    [
        (StartRunRequest, {"unexpected": 1}),
        (
            MiseEnPlaceCheckOut,
            {"code": "budget_available", "passed": True, "detail": "ok", "unexpected": 1},
        ),
        (
            MiseEnPlaceReportOut,
            {"checks": _four_checks(), "all_passed": True, "unexpected": 1},
        ),
    ],
)
def test_mise_en_place_schemas_forbid_extra_fields(model: type, kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        model(**kwargs)


def test_mise_en_place_check_out_code_is_a_closed_set() -> None:
    with pytest.raises(ValidationError):
        MiseEnPlaceCheckOut(code="not_a_real_code", passed=True, detail="x")  # type: ignore[arg-type]


def test_mise_en_place_report_out_round_trips_full_shape() -> None:
    """An ACTUAL round-trip: dump to JSON-compatible primitives and validate
    them back (review P22). The previous version only re-read attributes off
    the object it had just built, which is why it never caught that the
    persisted shape and this model had drifted apart (review P13)."""
    checks = list(_four_checks())
    checks[2] = MiseEnPlaceCheckOut(
        code="budget_available",
        passed=False,
        detail="over cap",
        suggested_action="Ajuster AGENTIVE_DRY_RUN_BUDGET_CAP_USD ou réduire le scope du workflow",
    )
    report = MiseEnPlaceReportOut(
        checks=checks, all_passed=False, bypassed=True, bypass_reason="incident P1"
    )

    payload = report.model_dump(mode="json")
    assert MiseEnPlaceReportOut.model_validate(payload) == report
    # This is exactly what `start_run` writes to `workflow_runs.mise_en_place`,
    # so the persisted document must carry `all_passed` for SQL audit queries.
    assert payload["all_passed"] is False
    assert payload["bypass_reason"] == "incident P1"
    assert len(payload["checks"]) == len(CHECK_CODES)


def test_mise_en_place_report_out_rejects_a_partial_report() -> None:
    with pytest.raises(ValidationError):
        MiseEnPlaceReportOut(checks=_four_checks()[:2], all_passed=True)


# ─── Story 4.6 T12.8 — the two run-control schemas ─────────────────────
#
# T12.8 was marked `[x]` and no such test existed: neither `RunControlResponse`
# nor `ResumeRunRequest` was named anywhere under `backend/tests/` (review lot
# 11). Both are HTTP contract surfaces — one is what every control call
# returns, the other is what `IG2` added to `resume`.


def test_run_control_response_rejects_unknown_fields() -> None:
    """`extra="forbid"` on a RESPONSE model is a typo-catcher for us, not for
    the client: it fails the build of a body carrying a field no consumer
    knows how to read."""
    from agentive_backend.features.workflow_engine.schemas import RunControlResponse

    with pytest.raises(ValidationError):
        RunControlResponse(run_id=uuid4(), status="running", controle_signal="pause")  # type: ignore[call-arg]


def test_run_control_response_status_vocabulary_matches_the_domain() -> None:
    """The literal here is a SECOND copy of the run-status vocabulary, and
    `domain.run_control.RUN_STATUSES` is the first. A status added to the
    state machine but not here makes the endpoint 500 on a run that reached
    it — the same split-brain `TERMINAL_STATUSES` produced before T5.4."""
    from typing import get_args

    from agentive_backend.features.workflow_engine.domain.run_control import RUN_STATUSES
    from agentive_backend.features.workflow_engine.schemas import RunControlResponse

    declared = get_args(RunControlResponse.model_fields["status"].annotation)
    assert set(declared) == set(RUN_STATUSES)


def test_run_control_response_defaults_control_signal_to_none() -> None:
    """An IMMEDIATE transition (`resume`, `cancel` on a paused run) has no
    pending request to report — the field is absent, not empty-stringed."""
    from agentive_backend.features.workflow_engine.schemas import RunControlResponse

    assert RunControlResponse(run_id=uuid4(), status="running").control_signal is None


def test_resume_run_request_defaults_to_no_bypass() -> None:
    """`POST /resume` with no body at all must be legal — the gate is the
    normal path and the bypass is the exception."""
    from agentive_backend.features.workflow_engine.schemas import ResumeRunRequest

    body = ResumeRunRequest()
    assert body.force is False
    assert body.reason is None


@pytest.mark.parametrize(
    ("force", "reason"),
    [
        (True, None),  # a bypass nobody has to justify
        (True, "   "),  # …nor one justified with whitespace
        (False, "cle restauree"),  # a reason that would be silently dropped
    ],
)
def test_resume_run_request_rejects_force_and_reason_apart(force: bool, reason: str | None) -> None:
    """Both directions, mirroring `StartRunRequest`. A bypass with no stated
    reason defeats the audit trail the field exists for (NFR8); a reason
    without a bypass would be accepted and then discarded, which reads to the
    caller exactly like it was recorded."""
    from agentive_backend.features.workflow_engine.schemas import ResumeRunRequest

    with pytest.raises(ValidationError):
        ResumeRunRequest(force=force, reason=reason)


def test_resume_run_request_accepts_a_justified_bypass() -> None:
    from agentive_backend.features.workflow_engine.schemas import ResumeRunRequest

    body = ResumeRunRequest(force=True, reason="MCP server retired, run must finish")
    assert body.force is True
    assert body.reason is not None


def test_resume_run_request_forbids_the_start_run_fields() -> None:
    """`ResumeRunRequest` is deliberately NOT a subclass of `StartRunRequest`
    and shares no base with it: a resume restarts from a checkpoint, so an
    `input` would be silently ignored. `extra="forbid"` is what turns that
    into a 422 instead of a surprise."""
    from agentive_backend.features.workflow_engine.schemas import ResumeRunRequest

    with pytest.raises(ValidationError):
        ResumeRunRequest(input={"task": "x"})  # type: ignore[call-arg]
