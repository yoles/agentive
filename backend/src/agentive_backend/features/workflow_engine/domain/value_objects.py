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

    ``node_outputs``/``node_metrics``/``routing_decisions``/``handoffs``/
    ``handoff_substitutions`` all use the stdlib ``operator.or_`` reducer (dict merge, Python 3.9+) — without it, a
    fan-out step where 2+ nodes complete in the same LangGraph "superstep"
    would have their state updates overwrite each other's key instead of
    merging (last-write-wins is LangGraph's default with no reducer). Node
    ids are guaranteed unique by construction (Story 4.1 AC1's duplicate-
    ``node_id`` check), so this is purely a merge concern, never a real
    key-collision to resolve.
    """

    task_input: dict[str, Any]
    correlation_id: str
    node_outputs: Annotated[dict[str, dict[str, Any] | None], operator.or_]
    node_metrics: Annotated[dict[str, dict[str, Any]], operator.or_]
    # Story 4.3 T4.2 — one entry per routing decision POINT (a node with at
    # least one conditional outgoing edge), keyed by that node's `node_id`.
    # Written by the emitting node's own step (`hybrid_router.decide_route`,
    # composed around `execute_agent_node` — T6.2), then only READ by
    # `_make_router` (T6.3). Absent for a run checkpointed before this story;
    # `_make_router` falls back to the pure deterministic resolution in that
    # case (AC4 — resuming a pre-4.3 run must not raise).
    routing_decisions: Annotated[dict[str, dict[str, Any]], operator.or_]
    # Story 4.7 T1.2 — one entry per node that (a) produced an output AND
    # (b) has at least one successor in the DAG, keyed by that node's
    # `node_id` (mirror `routing_decisions` exactly: same reducer, same
    # keying, same "one entry per relevant node" shape — a `dict`, not the
    # `handoffs[]` the epic's own prose uses, for reducer consistency with
    # the other three channels here). Written by `execute_agent_node` via
    # `summarize_handoff` (`engine/handoff.py`), then READ by
    # `_serialize_upstream`/`_build_user_message` (`engine/agent_node.py`)
    # instead of `node_outputs`, UNLESS a consuming template opts out via
    # `config["include_raw_previous_output"] = True` (AC2). `node_outputs`
    # itself is never touched by this channel — `condition_dsl`/`_make_router`
    # (Story 4.3) keep routing on the raw output, never on a summary.
    # Absent for a run checkpointed before this story, AND absent for any
    # node whose summarization failed (AC1) — both cases degrade the same
    # way: a per-key fallback onto that node's own `node_outputs` entry,
    # never a hard failure (AC2).
    handoffs: Annotated[dict[str, dict[str, Any]], operator.or_]

    # What each CONSUMING node's prompt actually substituted, keyed by that
    # consumer's `node_id` (review of 2026-09-12, B-01). Same reducer, same
    # shape; keyed by consumer where `handoffs` is keyed by producer, so the
    # two never collide on merge.
    #
    # `handoffs` answers "what did producing summaries cost"; this answers
    # "what did substituting them actually save", and only the second one can
    # honestly feed FR53's reduction ratio. Counting on the producer side
    # credited a saving three ways it never made: to a consumer that opted
    # out and read the raw output anyway; to an entry the
    # `MAX_UPSTREAM_OUTPUT_CHARS` cap dropped before it reached any prompt;
    # and, on a chain, ONCE for an output that `_serialize_upstream`'s
    # cumulative upstream view forwards to every downstream node in turn.
    # Written by `execute_agent_node` from what `_serialize_upstream` really
    # emitted, after truncation — never from what was produced.
    #
    # Absent for a run checkpointed before that review; the ratio then reads
    # `None`, the same honest "nothing measured" the zero-denominator case
    # already returns, never a fabricated figure.
    handoff_substitutions: Annotated[dict[str, dict[str, Any]], operator.or_]


__all__ = [
    "DomainValidationError",
    "WorkflowDag",
    "WorkflowEdge",
    "WorkflowNode",
    "WorkflowState",
]
