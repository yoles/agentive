"""Framework-free value objects for the Workflow Engine domain core (Story 4.1).

Pure Python (frozen dataclasses) — no Pydantic, no SQLAlchemy — mirrors the
posture of ``agent_registry/domain/value_objects.py`` (audit "domaine
anémique"). ``.import-linter`` compliance: no ``infra.*``/``sqlalchemy``/
``pydantic`` imports here (Contract 2/3).
"""

from __future__ import annotations

from dataclasses import dataclass
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


__all__ = ["DomainValidationError", "WorkflowDag", "WorkflowEdge", "WorkflowNode"]
