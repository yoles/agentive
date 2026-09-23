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
* :mod:`.dry_run` (Story 4.4) — predictive path/cost estimation for Dry Run:
  :func:`compute_probable_path` (structural + historical, never re-evaluates
  a branching condition — that needs a node's real ``own_output``, which
  does not exist pre-execution) and :func:`average_node_tokens`.
* :mod:`.mise_en_place` (Story 4.5) — pure report assembly for the
  pre-workflow hook: :class:`CheckResult`/:class:`MiseEnPlaceReport` and
  :func:`build_report`. The four checks themselves are I/O-bound and live in
  the sibling ``features/workflow_engine/mise_en_place.py`` orchestrator, not
  here.
* :mod:`.run_control` (Story 4.6) — the pause/resume/cancel state machine:
  :class:`Transition`, :func:`resolve_transition` (the single source of every
  409 this feature returns) and :data:`TERMINAL_STATUSES` (the single
  definition, imported by the SSE loop rather than redefined there).
* :mod:`.error_policy` (Story 4.6, défer D13) — node-level retry policy read
  defensively from free-form JSONB, plus the bounded :func:`backoff_delay_s`.
  Distinct from Story 1.6's provider-chain fallback and from Story 9.5's
  intra-provider retry — see the module docstring's three-mechanism table.
* :mod:`.provider_chain` (Story 4.6, défer D12) — per-agent provider chain
  intersected with the providers actually registered in this process. Shared
  by the runtime (``engine/agent_node.py``) and the Mise en Place pre-flight
  check, so the check predicts what the runtime DOES.

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
from agentive_backend.features.workflow_engine.domain.dry_run import (
    END_SENTINEL,
    NodeMetricSample,
    ProbablePathResult,
    RoutingHistory,
    average_node_tokens,
    classify_decision_points,
    compute_probable_path,
    find_root_nodes,
)
from agentive_backend.features.workflow_engine.domain.error_policy import (
    MAX_ERROR_POLICY_RETRIES,
    BackoffStrategy,
    OnTimeout,
    ResolvedErrorPolicy,
    backoff_delay_s,
    resolve_error_policy,
)
from agentive_backend.features.workflow_engine.domain.mise_en_place import (
    CheckCode,
    CheckResult,
    MiseEnPlaceReport,
    build_report,
)
from agentive_backend.features.workflow_engine.domain.provider_chain import (
    ChainResolution,
    resolve_provider_chain,
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
from agentive_backend.features.workflow_engine.domain.run_control import (
    RUN_STATUSES,
    TERMINAL_STATUSES,
    ControlSignal,
    RunAction,
    RunStatus,
    Transition,
    resolve_transition,
)
from agentive_backend.features.workflow_engine.domain.value_objects import (
    DomainValidationError,
    WorkflowDag,
    WorkflowEdge,
    WorkflowNode,
    WorkflowState,
)

__all__ = [
    "END_SENTINEL",
    "MAX_ERROR_POLICY_RETRIES",
    "RUN_STATUSES",
    "TERMINAL_STATUSES",
    "BackoffStrategy",
    "ChainResolution",
    "CheckCode",
    "CheckResult",
    "ControlSignal",
    "DomainValidationError",
    "MiseEnPlaceReport",
    "NodeMetricSample",
    "OnTimeout",
    "ParsedCondition",
    "ProbablePathResult",
    "ResolvedErrorPolicy",
    "RoutingContext",
    "RoutingDecision",
    "RoutingEscalationError",
    "RoutingHistory",
    "RoutingRule",
    "RuleMatch",
    "RulePenalty",
    "RuleVerdict",
    "RunAction",
    "RunStatus",
    "Transition",
    "WorkflowDag",
    "WorkflowEdge",
    "WorkflowNode",
    "WorkflowState",
    "average_node_tokens",
    "backoff_delay_s",
    "build_report",
    "classify_decision_points",
    "compute_confidence",
    "compute_probable_path",
    "detect_cycle",
    "evaluate",
    "evaluate_rules",
    "find_dangling_edges",
    "find_duplicate_node_ids",
    "find_root_nodes",
    "parse",
    "resolve_error_policy",
    "resolve_provider_chain",
    "resolve_transition",
]
