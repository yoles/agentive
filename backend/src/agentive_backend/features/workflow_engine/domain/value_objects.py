"""Framework-free value objects for the Workflow Engine domain core (Story 4.1).

Pure Python (frozen dataclasses) — no Pydantic, no SQLAlchemy — mirrors the
posture of ``agent_registry/domain/value_objects.py`` (audit "domaine
anémique"). ``.import-linter`` compliance: no ``infra.*``/``sqlalchemy``/
``pydantic`` imports here (Contract 2/3).
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict
from uuid import UUID


class DomainValidationError(ValueError):
    """Raised when domain-layer validation fails (duplicate node, cycle,
    invalid DSL, …). Framework-free — the service layer translates this into
    ``shared.exceptions.ValidationError`` (422) at the HTTP boundary. Mirrors
    ``agent_registry.domain.value_objects.DomainValidationError``; not shared
    across the two modules because ``.import-linter`` Contract 1 forbids
    ``features.workflow_engine`` from importing ``features.agent_registry``.
    """


@dataclass(frozen=True, slots=True)
class WorkflowNode:
    """One participant in the DAG — a ``node_id`` bound to an agent template."""

    node_id: str
    agent_template_id: UUID


@dataclass(frozen=True, slots=True)
class WorkflowEdge:
    """One directed edge in the DAG, with an optional branching condition."""

    from_node_id: str
    to_node_id: str
    condition: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowDag:
    """The full DAG — nodes + edges, as submitted by ``POST /api/v1/workflows``."""

    nodes: tuple[WorkflowNode, ...]
    edges: tuple[WorkflowEdge, ...]


class WorkflowState(TypedDict, total=False):
    """LangGraph state-channel schema for one workflow run (Story 4.2 T3.3).

    ``node_outputs``/``node_metrics`` use the stdlib ``operator.or_`` reducer
    (dict merge, Python 3.9+) — without it, a fan-out step where 2+ nodes
    complete in the same LangGraph "superstep" would have their state
    updates overwrite each other's key instead of merging (last-write-wins
    is LangGraph's default with no reducer). Node ids are guaranteed unique
    by construction (Story 4.1 AC1's duplicate-``node_id`` check), so this
    is purely a merge concern, never a real key-collision to resolve.
    """

    task_input: dict[str, Any]
    correlation_id: str
    node_outputs: Annotated[dict[str, dict[str, Any] | None], operator.or_]
    node_metrics: Annotated[dict[str, dict[str, Any]], operator.or_]


__all__ = [
    "DomainValidationError",
    "WorkflowDag",
    "WorkflowEdge",
    "WorkflowNode",
    "WorkflowState",
]
