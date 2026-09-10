"""Domain core for the Workflow Engine bounded context (Story 4.1, 4.2).

Framework-free layer (dataclasses/TypedDicts only, no Pydantic, no
SQLAlchemy) for the pure-logic modules, mirror of ``agent_registry/domain/``:

* :mod:`.value_objects` — ``WorkflowNode``/``WorkflowEdge``/``WorkflowDag``/
  ``WorkflowState``.
* :mod:`.dag` — structural validations (duplicate node ids, dangling edges,
  cycle detection via ``graphlib.TopologicalSorter``).
* :mod:`.condition_dsl` — restricted branching-condition parser + runtime
  evaluator (``output.<field> <op> <literal>``, never ``eval``/``exec``).

Story 4.2's LangGraph-coupled modules (``graph_builder``, ``agent_node``)
live in the sibling :mod:`..engine` package, NOT here — see its docstring.
Keeping them here made ``import domain`` pull LangGraph in transitively,
which is exactly the property this package exists to avoid.
"""

from __future__ import annotations

from agentive_backend.features.workflow_engine.domain.condition_dsl import (
    ParsedCondition,
    evaluate,
    parse,
)
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
    WorkflowState,
)

__all__ = [
    "DomainValidationError",
    "ParsedCondition",
    "WorkflowDag",
    "WorkflowEdge",
    "WorkflowNode",
    "WorkflowState",
    "detect_cycle",
    "evaluate",
    "find_dangling_edges",
    "find_duplicate_node_ids",
    "parse",
]
