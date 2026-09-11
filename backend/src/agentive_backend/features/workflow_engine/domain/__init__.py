"""Domain core for the Workflow Engine bounded context (Story 4.1, 4.2).

Framework-free layer (dataclasses/TypedDicts only, no Pydantic, no
SQLAlchemy) for the pure-logic modules, mirror of ``agent_registry/domain/``:

* :mod:`.value_objects` — ``WorkflowNode``/``WorkflowEdge``/``WorkflowDag``/
  ``WorkflowState``.
* :mod:`.dag` — structural validations (duplicate node ids, dangling edges,
  cycle detection via ``graphlib.TopologicalSorter``).
* :mod:`.condition_dsl` — restricted branching-condition parser + runtime
  evaluator (``<namespace>.<field> <op> <literal>``, never ``eval``/``exec``).
* :mod:`.routing_rules` (Story 4.3) — declarative rule scoring for the
  hybrid router: :class:`RoutingContext`/:class:`RoutingRule`/
  :class:`RoutingDecision` and the pure ``compute_confidence``/
  ``evaluate_rules`` functions. No YAML, no I/O — the catalog loader
  (``features/workflow_engine/routing_catalog.py``) is a sibling of
  ``domain/``, not part of it, because it imports Pydantic and ``yaml``.

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
from agentive_backend.features.workflow_engine.domain.routing_rules import (
    RoutingContext,
    RoutingDecision,
    RoutingEscalationError,
    RoutingRule,
    RuleMatch,
    RulePenalty,
    RuleVerdict,
    compute_confidence,
    evaluate_rules,
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
    "RoutingContext",
    "RoutingDecision",
    "RoutingEscalationError",
    "RoutingRule",
    "RuleMatch",
    "RulePenalty",
    "RuleVerdict",
    "WorkflowDag",
    "WorkflowEdge",
    "WorkflowNode",
    "WorkflowState",
    "compute_confidence",
    "detect_cycle",
    "evaluate",
    "evaluate_rules",
    "find_dangling_edges",
    "find_duplicate_node_ids",
    "parse",
]
