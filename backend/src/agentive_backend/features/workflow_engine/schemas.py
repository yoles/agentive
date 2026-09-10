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


__all__ = [
    "CreateWorkflowRequest",
    "CreateWorkflowResponse",
    "DiversityWarning",
    "StartRunRequest",
    "StartRunResponse",
    "WorkflowEdgeRequest",
    "WorkflowNodeRequest",
]
