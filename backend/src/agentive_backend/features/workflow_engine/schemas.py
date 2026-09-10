"""Pydantic HTTP schemas — Workflow Engine (Story 4.1 AC1-AC4)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Defensive caps — no DAG-builder UI exists yet (Story 4.1 anti-scope) and no
# workflow this MVP targets needs more; mirrors the `tool_ids` precedent in
# `agent_registry/schemas.py` (max_length=100, "Sprint 1 we don't expect
# more than a...").
_MAX_NODES = 100
_MAX_EDGES = 200


class WorkflowNodeRequest(BaseModel):
    """One participant node in the submitted DAG."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=100)
    agent_template_id: UUID


class WorkflowEdgeRequest(BaseModel):
    """One directed edge in the submitted DAG, with an optional branching condition."""

    model_config = ConfigDict(extra="forbid")

    from_node_id: str = Field(min_length=1, max_length=100)
    to_node_id: str = Field(min_length=1, max_length=100)
    condition: str | None = Field(default=None, max_length=500)


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


__all__ = [
    "CreateWorkflowRequest",
    "CreateWorkflowResponse",
    "DiversityWarning",
    "WorkflowEdgeRequest",
    "WorkflowNodeRequest",
]
