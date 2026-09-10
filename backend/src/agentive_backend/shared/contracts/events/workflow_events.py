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

Naming note (D91 point 3): the epic's literal AC3 wording ("workflow_resumed")
does not fit ``shared/event_bus/naming.py``'s strict 3-segment
``module.entity.action`` grammar — ``workflow_engine.workflow_run.resumed``
is the compliant form, mirror of the ``workflow.created`` rename already done
in Story 4.1.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar, Literal
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


__all__ = [
    "WorkflowCreatedEvent",
    "WorkflowRunCompletedEvent",
    "WorkflowRunFailedEvent",
    "WorkflowRunResumedEvent",
    "WorkflowRunStartedEvent",
    "WorkflowRunStepCompletedEvent",
]
