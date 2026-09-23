"""LangGraph execution layer for the Workflow Engine (Story 4.2).

Separate from :mod:`..domain` on purpose. ``domain/`` is the framework-free
core — dataclasses and TypedDicts, no Pydantic, no SQLAlchemy, no LangGraph —
mirroring ``agent_registry/domain/``, and its whole value is that the pure
logic (``value_objects``, ``dag``, ``condition_dsl``) can be reasoned about
and tested without a framework in sight.

Story 4.2 originally placed these two modules inside ``domain/`` and amended
that package's docstring to admit the exception. The effect was that
importing ``domain`` — for a DAG value object, say — pulled LangGraph in
transitively, so the pure modules were no longer importable, let alone
testable, without it. The review restored the boundary by moving the modules
rather than by re-describing the package.

* :mod:`.graph_builder` — compiles a persisted DAG into a LangGraph ``StateGraph``.
* :mod:`.agent_node` — one node's LLM completion.
* :mod:`.hybrid_router` — the routing decision itself (Story 4.3): the 4.1/4.2
  branching DSL, then declarative rules, then LLM escalation. Deliberately
  NOT re-exported below: ``hybrid_router`` and ``routing_catalog`` are
  feature-internal, and only result types cross the package boundary
  (``architecture.md#Public API par feature``).
"""

from __future__ import annotations

from agentive_backend.features.workflow_engine.engine.agent_node import (
    NODE_TIMEOUT_S,
    execute_agent_node,
)
from agentive_backend.features.workflow_engine.engine.graph_builder import build_state_graph

__all__ = ["NODE_TIMEOUT_S", "build_state_graph", "execute_agent_node"]
