"""Domain core for the Workflow Engine bounded context (Story 4.1).

Framework-free layer (dataclasses only, no Pydantic, no SQLAlchemy), mirror
of ``agent_registry/domain/``:

* :mod:`.value_objects` — ``WorkflowNode``/``WorkflowEdge``/``WorkflowDag``.
* :mod:`.dag` — structural validations (duplicate node ids, dangling edges,
  cycle detection via ``graphlib.TopologicalSorter``).
* :mod:`.condition_dsl` — restricted branching-condition parser
  (``output.<field> <op> <literal>``, never ``eval``/``exec``).
"""

from __future__ import annotations

from agentive_backend.features.workflow_engine.domain.condition_dsl import ParsedCondition, parse
from agentive_backend.features.workflow_engine.domain.dag import (
    detect_cycle,
    find_dangling_edges,
    find_duplicate_node_ids,
)
from agentive_backend.features.workflow_engine.domain.value_objects import (
    DomainValidationError,
    WorkflowDag,
    WorkflowEdge,
    WorkflowNode,
)

__all__ = [
    "DomainValidationError",
    "ParsedCondition",
    "WorkflowDag",
    "WorkflowEdge",
    "WorkflowNode",
    "detect_cycle",
    "find_dangling_edges",
    "find_duplicate_node_ids",
    "parse",
]
