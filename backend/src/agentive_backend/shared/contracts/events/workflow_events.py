"""Workflow lifecycle events — Epic 4 Workflow Orchestration Engine.

Naming convention follows Story 1.7 pattern ``{module}.{object}.{verb}``.
Events shipped to date:

* ``workflow_engine.workflow.created`` (Story 4.1) — emitted after a new
  ``workflows`` row is committed via ``WorkflowService.create_workflow``.

Defer Story 4.2+ : run lifecycle events (``workflow_engine.workflow_run.*``)
once the execution engine exists.
"""

from __future__ import annotations

from typing import ClassVar
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


__all__ = ["WorkflowCreatedEvent"]
