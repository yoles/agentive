"""Pydantic HTTP schemas — Workflow Engine (Story 4.1 AC1-AC4)."""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Defensive caps — no DAG-builder UI exists yet (Story 4.1 anti-scope) and no
# workflow this MVP targets needs more; mirrors the `tool_ids` precedent in
# `agent_registry/schemas.py` (max_length=100, "Sprint 1 we don't expect
# more than a...").
_MAX_NODES = 100
_MAX_EDGES = 200

# LangGraph reserves `__start__`/`__end__` (and other dunder-wrapped names)
# for its own graph sentinels: `StateGraph.add_node` REFUSES them. Without
# this guard a workflow naming a node `__start__` was accepted at creation
# (Story 4.1 only bounded the length) and then blew up at `build_state_graph`
# time — i.e. at RUN time, on a background task, for every run of that
# workflow forever. Rejecting at creation turns a permanently broken workflow
# into an immediate, actionable 422.
_RESERVED_NODE_ID_RE = re.compile(r"^__.*__$")


def _reject_reserved_node_id(value: str) -> str:
    if _RESERVED_NODE_ID_RE.match(value):
        raise ValueError(
            f"node_id {value!r} is reserved by the workflow engine "
            "(names wrapped in double underscores are not allowed)"
        )
    return value


class WorkflowNodeRequest(BaseModel):
    """One participant node in the submitted DAG."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=100)
    agent_template_id: UUID

    _check_node_id = field_validator("node_id", mode="after")(_reject_reserved_node_id)


class WorkflowEdgeRequest(BaseModel):
    """One directed edge in the submitted DAG, with an optional branching condition."""

    model_config = ConfigDict(extra="forbid")

    from_node_id: str = Field(min_length=1, max_length=100)
    to_node_id: str = Field(min_length=1, max_length=100)
    condition: str | None = Field(default=None, max_length=500)

    # Edges pointing at a reserved name would otherwise surface as the much
    # vaguer "edge references undeclared node" error.
    _check_endpoints = field_validator("from_node_id", "to_node_id", mode="after")(
        _reject_reserved_node_id
    )


class CreateWorkflowRequest(BaseModel):
    """Body of ``POST /api/v1/workflows``.

    A single-node DAG with no edges is a valid mono-agent workflow.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    nodes: list[WorkflowNodeRequest] = Field(min_length=1, max_length=_MAX_NODES)
    edges: list[WorkflowEdgeRequest] = Field(default_factory=list, max_length=_MAX_EDGES)

    @field_validator("name", mode="after")
    @classmethod
    def _strip_and_revalidate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank or whitespace-only")
        return stripped


class DiversityWarning(BaseModel):
    """Non-blocking Contrôleur/Producteur LLM-diversity alert (AC4, FR15, D84)."""

    model_config = ConfigDict(extra="forbid")

    code: Literal["llm_diversity"] = "llm_diversity"
    controller_node_id: str
    producer_node_id: str
    controller_template_id: UUID
    producer_template_id: UUID
    reason: str


class CreateWorkflowResponse(BaseModel):
    """Response of ``POST /api/v1/workflows`` — 201 Created."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: UUID
    version: int
    warnings: list[DiversityWarning] = Field(default_factory=list)


class StartRunRequest(BaseModel):
    """Body of ``POST /api/v1/workflows/{workflow_id}/runs`` (Story 4.2 AC1).

    ``input`` is free-form JSON — the workflow's initial ``task_input``,
    passed to the first node(s) unmodified.
    """

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict)


class StartRunResponse(BaseModel):
    """Response of ``POST /api/v1/workflows/{workflow_id}/runs`` — 201 Created.

    Returned IMMEDIATELY, before the run progresses (AC1) — execution
    happens in a fire-and-forget background task.

    ``warnings`` carries the Contrôleur/Producteur LLM-diversity check re-run
    at run start, the second evaluation point ``epics.md`` assigns to this
    story. Non-blocking, exactly like the creation-time check of 4.1 AC4: an
    empty list is the normal case, a non-empty one still comes with a 201.
    It is re-evaluated here rather than trusted from creation because
    ``agent_templates`` rows are mutable — a workflow validated as diverse
    can be running two identical models by the time anyone starts it.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: Literal["running"] = "running"
    warnings: list[DiversityWarning] = Field(default_factory=list)


class RoutingStatsResponse(BaseModel):
    """Response of ``GET /api/v1/workflows/{workflow_id}/routing-stats``
    (Story 4.3 AC3 T10.2) — ``% routages déterministes vs LLM`` aggregated
    across every run of the workflow.

    ``deterministic_pct`` is ``None`` when ``deterministic + llm_escalated
    == 0`` — a legitimate state (no decision point was ever reached, e.g. a
    purely sequential workflow, or a workflow with zero runs), never a
    division-by-zero to paper over.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: UUID
    #: EVERY run of this workflow, whatever its status — including runs that
    #: predate Story 4.3, runs still `running`, and runs that failed. It is
    #: therefore NOT the denominator of `deterministic_pct`: a workflow can
    #: legitimately report many runs counted and zero decisions. The ratio's
    #: denominator is `deterministic + llm_escalated`, which counts DECISIONS,
    #: not runs.
    runs_counted: int = Field(ge=0)
    deterministic: int = Field(ge=0)
    llm_escalated: int = Field(ge=0)
    deterministic_pct: float | None = None


class DryRunRequest(BaseModel):
    """Body of ``POST /api/v1/workflows/{workflow_id}/dry-run`` (Story 4.4 AC1).

    Mirror of :class:`StartRunRequest` — the same ``task_input`` the workflow
    would receive on a real run. Never persisted: a Dry Run creates no
    ``workflow_runs`` row.

    **``input`` does not influence the estimate** (review fix P16). It is
    accepted so a caller can send the exact body it would POST to
    ``/runs``, but the estimation is structural + historical by design
    (Dev Notes § Algorithme ``probable_path``): with zero real LLM call
    there is no way to learn what THIS input would produce. A 10-character
    input and a 200 KB one therefore return identical token figures. Stated
    here because this is the public contract — burying it in the service
    docstring left every API consumer to discover it by experiment.
    """

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict)


class ProviderTokenEstimate(BaseModel):
    """Token/cost estimate aggregated for one resolved LLM provider
    (Story 4.4 AC1) — one entry per key of
    :attr:`DryRunResponse.token_estimate_per_provider`."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    #: ``str``, never ``Decimal`` — mirror ``RoutingDecision.llm_cost_usd``
    #: (Story 4.3): this value crosses a JSON boundary, whose serializer
    #: raises on a raw ``Decimal``. ``None`` when no model in this provider
    #: group resolved a price (never a fabricated ``"0"``).
    cost_usd: str | None = None


class DryRunAgentInvolvement(BaseModel):
    """One node structurally reachable by the Dry Run (Story 4.4 AC1) —
    a superset of the nodes on ``probable_path``: a node that might run
    must be priced even when it is not on the single representative path."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    agent_template_id: UUID
    on_probable_path: bool


class DryRunRisk(BaseModel):
    """One identified risk (Story 4.4 AC1/AC3). ``code`` is a CLOSED set —
    exactly the risk types this story computes (no "recrutement dynamique"
    code: no dynamic-recruitment mechanism exists in this repo, see Story
    4.4 Dev Notes § Divergences assumées).

    Three of the six codes were added by the review (IG1/IG2/IG4). The
    original three left the response unable to say that an estimate was
    PARTIAL: a node whose model carried no price was dropped from the
    figures entirely, `no_execution_history` only ever fired for a workflow
    that had never run at all, and a historical fan-out decision was
    silently narrowed to one branch. Every one of those made the response
    look more complete than it was — the opposite of what a number meant
    for budgeting should do. Story 9.4 consumes these to know whether it
    can trust a total before blocking on it.
    """

    model_config = ConfigDict(extra="forbid")

    code: Literal[
        # A decision point with no usable historical majority — the path
        # guesses at this node.
        "routing_decision_uncertain",
        # The winning historical decision named several targets: the engine
        # would fan out here, `probable_path` can only show one of them.
        "routing_decision_multi_target",
        # The workflow has never run: every figure is heuristic.
        "no_execution_history",
        # THIS node had no usable history, even though the workflow has run
        # (a node added since, or one only reached on a rare branch).
        "node_estimate_from_fallback",
        # This node's model is absent from every pricing table, so its
        # tokens AND cost are missing from the totals.
        "model_price_unresolved",
        # `cost_estimate_usd` is over the configured cap (AC3).
        "budget_cap_exceeded",
    ]
    node_id: str | None = None
    detail: str
    #: The two figures AC3 asks `budget_cap_exceeded` to carry ("le montant
    #: estimé et le seuil dépassé"), as machine-readable fields rather than
    #: prose (review, BS1). T4.1 specified this model as
    #: ``{code, node_id, detail}``, which left nowhere to put them — so they
    #: went into the English `detail` string, and the Epic 6 Dialog this
    #: contract exists to prepare would have had to parse it back out. Same
    #: `str`-not-`Decimal` convention as everywhere else in this response:
    #: the JSON serializer raises on a raw `Decimal`.
    #:
    #: Populated only for `budget_cap_exceeded`; `None` on every other code.
    #: `detail` still states both, for a human reading the raw response.
    estimated_usd: str | None = None
    threshold_usd: str | None = None


class DryRunResponse(BaseModel):
    """Response of ``POST /api/v1/workflows/{workflow_id}/dry-run`` — 200 OK
    (Story 4.4 AC1). Never 201: unlike ``POST /workflows`` and
    ``POST /workflows/{id}/runs``, this endpoint creates no resource."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: UUID
    probable_path: list[str]
    agents_involved: list[DryRunAgentInvolvement]
    token_estimate_per_provider: dict[str, ProviderTokenEstimate]
    #: UPPER BOUND: every node in ``agents_involved``, including branches
    #: that are mutually exclusive at runtime. A 5-way exclusive decision
    #: bills all five here while a real run pays one — deliberate, since
    #: excluding them would understate what the workflow CAN cost.
    #: ``None`` when NO node resolved a price — never a fabricated ``"0"``
    #: (Story 4.4 Dev Notes § Pièges connus #3).
    cost_estimate_usd: str | None
    #: EXPECTED cost: only the nodes on ``probable_path``. Added by the
    #: review (IG7) because the upper bound alone is neither the expected
    #: spend nor an announced bound, which makes it hard to act on for a
    #: `[Lancer]`/`[Annuler]` decision. Both are reported so the caller
    #: picks; `on_probable_path` already made the split available per node.
    #: ``None`` on the same terms as ``cost_estimate_usd``.
    probable_path_cost_usd: str | None = None
    identified_risks: list[DryRunRisk]


__all__ = [
    "CreateWorkflowRequest",
    "CreateWorkflowResponse",
    "DiversityWarning",
    "DryRunAgentInvolvement",
    "DryRunRequest",
    "DryRunResponse",
    "DryRunRisk",
    "ProviderTokenEstimate",
    "RoutingStatsResponse",
    "StartRunRequest",
    "StartRunResponse",
    "WorkflowEdgeRequest",
    "WorkflowNodeRequest",
]
