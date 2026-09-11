"""Workflow lifecycle events — Epic 4 Workflow Orchestration Engine.

Naming convention follows Story 1.7 pattern ``{module}.{object}.{verb}``.
Events shipped to date:

* ``workflow_engine.workflow.created`` (Story 4.1) — emitted after a new
  ``workflows`` row is committed via ``WorkflowService.create_workflow``.
* ``workflow_engine.workflow_run.started`` (Story 4.2 AC1) — a run starts
  executing (``WorkflowExecutionService.start_run``).
* ``workflow_engine.workflow_run.step_completed`` (Story 4.2 AC2) — one node
  finishes successfully.
* ``workflow_engine.workflow_run.resumed`` (Story 4.2 AC3) — the recovery
  worker resumes an orphaned run from its last LangGraph checkpoint.
* ``workflow_engine.workflow_run.completed`` (Story 4.2 AC4) — a run reaches
  ``END`` on every branch.
* ``workflow_engine.workflow_run.failed`` (Story 4.2 AC4) — a node raised a
  non-recoverable error, terminating the run.
* ``workflow_engine.workflow_run.routing_escalated`` (Story 4.3 AC2/AC3) —
  a routing decision point could not be resolved deterministically (DSL
  silent, no rule cleared the confidence threshold) and escalated to the
  lightweight LLM.
* ``workflow_engine.workflow_run.mise_en_place_bypassed`` (Story 4.5 AC3) —
  the caller started a run with ``force=true`` while one or more pre-workflow
  checks were failing. The first event in this repo representing an explicit
  user bypass with a stated reason (grep-confirmed at story time — no prior
  ``_bypassed``/``_forced`` event existed).
* ``workflow_engine.workflow_run.mise_en_place_refused`` (Story 4.5 AC2,
  review BS2) — the symmetric case: a launch REFUSED because a check failed
  and no ``force`` was given. It exists because AC2 forbids creating a
  ``workflow_runs`` row for a refused launch, so the report has nowhere to
  be persisted on that table; the outbox is where the refusal is traced
  instead. Carries no ``run_id`` — there is no run.

Naming note (D91 point 3): the epic's literal AC3 wording ("workflow_resumed")
does not fit ``shared/event_bus/naming.py``'s strict 3-segment
``module.entity.action`` grammar — ``workflow_engine.workflow_run.resumed``
is the compliant form, mirror of the ``workflow.created`` rename already done
in Story 4.1.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class WorkflowCreatedEvent(BaseModel):
    """Published after a new ``workflows`` row is durably committed (Story 4.1 AC2).

    Same audit-bypass pattern as the other Sprint 1-3 lifecycle events — lives
    only in ``outbox_events`` until Story 9.1 wires ``AuditEventRepo``.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow.created"

    workflow_id: UUID
    name: str = Field(min_length=1, max_length=255)
    version: int = Field(ge=1)
    node_count: int = Field(ge=0)
    actor: str = Field(default="system", description="user_id or 'system' for unattended creation")
    tenant_id: UUID | None = None


class WorkflowRunStartedEvent(BaseModel):
    """Published when a run starts executing (Story 4.2 AC1)."""

    event_type: ClassVar[str] = "workflow_engine.workflow_run.started"

    run_id: UUID
    workflow_id: UUID
    actor: str = Field(default="system", description="user_id or 'system' for unattended runs")
    tenant_id: UUID | None = None


class WorkflowRunStepCompletedEvent(BaseModel):
    """Published after each node terminates successfully (Story 4.2 AC2).

    ``status`` is always ``"success"`` — a node failure is a separate
    ``workflow_run.failed`` event (T6.5), not a "step in error" state.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.step_completed"

    run_id: UUID
    workflow_id: UUID
    node_id: str = Field(min_length=1, max_length=100)
    status: Literal["success"] = "success"
    duration_ms: int = Field(ge=0)
    tenant_id: UUID | None = None


class WorkflowRunResumedEvent(BaseModel):
    """Published by the recovery worker before relaunching an orphaned run
    (Story 4.2 AC3)."""

    event_type: ClassVar[str] = "workflow_engine.workflow_run.resumed"

    run_id: UUID
    workflow_id: UUID
    resumed_from_node_id: str | None = Field(
        default=None, description="best-effort — None if not determinable"
    )
    tenant_id: UUID | None = None


class WorkflowRunCompletedEvent(BaseModel):
    """Published when a run reaches ``END`` on every branch (Story 4.2 AC4)."""

    event_type: ClassVar[str] = "workflow_engine.workflow_run.completed"

    run_id: UUID
    workflow_id: UUID
    total_duration_ms: int = Field(ge=0)
    total_cost_usd: Decimal | None = None
    tenant_id: UUID | None = None


class WorkflowRunFailedEvent(BaseModel):
    """Published when a node raises a non-recoverable error (Story 4.2 AC4).

    Partial metrics from already-completed nodes remain aggregated in
    ``workflow_runs.metrics`` — this event only carries the failure summary.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.failed"

    run_id: UUID
    workflow_id: UUID
    failed_node_id: str | None = None
    error_summary: str = Field(max_length=500)
    tenant_id: UUID | None = None


class WorkflowRunRoutingEscalatedEvent(BaseModel):
    """Published when a routing decision point escalates to the lightweight
    LLM (Story 4.3 AC2/AC3 — T7.1).

    Streamed as-is to SSE clients by the Story 4.2 endpoint's
    ``_RUN_EVENT_PATTERN`` (``workflow_engine\\.workflow_run\\.\\w+``) —
    deliberately NOT added to ``router.py``'s ``_TERMINAL_EVENT_SUFFIXES``
    (T7.4), and deliberately WITHOUT ``own_output`` in the payload (T7.3): a
    node's output can be arbitrarily large, and the SSE queue is bounded
    with drop journalisé — a fat payload here would turn an escalation burst
    into dropped frames. ``context`` carries only the scalar counts of
    :class:`~agentive_backend.features.workflow_engine.domain.routing_rules.RoutingContext`
    (never ``own_output``), which is exactly the raw material the Growth-phase
    rule-learning innovation (MVP #1, anti-scope here) needs.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.routing_escalated"

    run_id: UUID
    workflow_id: UUID
    node_id: str = Field(min_length=1, max_length=100)
    candidates: list[str] = Field(default_factory=list)
    decision_target: list[str] = Field(default_factory=list)
    confidence_best: float | None = None
    rule_id_best: str | None = None
    reason: str = Field(max_length=500)
    llm_model: str = Field(min_length=1)
    llm_latency_ms: int = Field(ge=0)
    context: dict[str, object] = Field(default_factory=dict)
    tenant_id: UUID | None = None


class WorkflowRunMiseEnPlaceBypassedEvent(BaseModel):
    """Published when a run starts with ``force=true`` despite one or more
    failing Mise en Place checks (Story 4.5 AC3) — audit trail for an
    explicit human bypass (NFR8).

    ``actor``, mirror :class:`WorkflowCreatedEvent`/:class:`WorkflowRunStartedEvent`
    — defaults to ``"system"`` under the same Sprint-1 convention (no
    authenticated-actor propagation anywhere yet), but this is precisely the
    event where a real actor will matter most once available.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.mise_en_place_bypassed"

    run_id: UUID
    workflow_id: UUID
    reason: str = Field(min_length=1, max_length=2000)
    failed_checks: list[str] = Field(default_factory=list)
    actor: str = Field(default="system", description="user_id or 'system' for unattended runs")
    tenant_id: UUID | None = None


class WorkflowRunMiseEnPlaceRefusedEvent(BaseModel):
    """Published when a launch is REFUSED because a Mise en Place check
    failed and the caller did not pass ``force`` (Story 4.5 AC2, review BS2).

    Why an event and not a row: AC2 requires that no ``workflow_runs`` row
    exist for a refused launch, so the report has nowhere to live on that
    table — AC1's "persisted whether the workflow starts or not" cannot hold
    there, and AC2 is the stronger requirement. Inventing a ``refused`` run
    row would leak a phantom run into run listings, the recovery worker's
    stale sweep, and routing stats. The outbox already carries the audit
    trail Trace Explorer consumes (NFR8/NFR15), and this story already
    publishes its sibling ``mise_en_place_bypassed`` there — a refusal is
    exactly as much an auditable decision as a bypass.

    Deliberately carries NO ``run_id``: there is no run, and the shape says
    so rather than fabricating one.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.mise_en_place_refused"

    workflow_id: UUID
    #: Codes of the checks that failed, i.e. why the launch was refused.
    failed_checks: list[str] = Field(default_factory=list)
    #: Human-readable summary — the same text the caller received in the
    #: RFC 7807 `detail`.
    detail: str = Field(default="", max_length=4000)
    #: Whether EVERY failing check was transient (review BS5) — mirrors the
    #: status the caller got: `True` → 503, `False` → 422.
    retryable: bool = False
    #: The full report as persisted-shaped JSON, so a consumer sees exactly
    #: what a started run's `workflow_runs.mise_en_place` would have held.
    mise_en_place: dict[str, Any] = Field(default_factory=dict)
    actor: str = Field(default="system", description="user_id or 'system' for unattended runs")
    tenant_id: UUID | None = None


__all__ = [
    "WorkflowCreatedEvent",
    "WorkflowRunCompletedEvent",
    "WorkflowRunFailedEvent",
    "WorkflowRunMiseEnPlaceBypassedEvent",
    "WorkflowRunMiseEnPlaceRefusedEvent",
    "WorkflowRunResumedEvent",
    "WorkflowRunRoutingEscalatedEvent",
    "WorkflowRunStartedEvent",
    "WorkflowRunStepCompletedEvent",
]
