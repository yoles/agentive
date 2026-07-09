"""Agent lifecycle events — Epic M2 Agent Registry (Story 2.1+).

Naming convention follows Story 1.7 pattern ``{module}.{object}.{verb}``.
Events shipped to date :

* ``agent_registry.agent_template.created`` (Story 2.1) — emitted after a new
  ``agent_templates`` row is committed via
  ``AgentRegistryService.create_template``.
* ``agent_registry.agent_template.updated`` (Story 2.2) — emitted after a config
  edit / version bump via ``AgentRegistryService.update_template``.
* ``agent_registry.agent_instance.created`` (Story 2.4) — emitted after a new
  ``agent_instances`` row is committed via
  ``AgentRegistryService.instantiate_from_template``.

Defer Story 4.x : ``agent_registry.agent_instance.completed`` (workflow_engine
signals the run end with the final status — needs the workflow_engine
runtime to exist first).

Story 2.1 §"Pièges connus" #5 — the object on the *template* events
is ``agent_template`` (not ``agent``). Template ↔ instance distinction
landed Story 2.4 — the new events use ``agent_instance``.
"""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field


class AgentTemplateCreatedEvent(BaseModel):
    """Published after a new ``agent_templates`` row is durably committed.

    The handler chain (Story 9.1+) will eventually persist this in
    ``audit_events`` via ``AuditEventRepo.record()``. Until then it lives
    only in ``outbox_events`` (the canonical bypass pattern documented in
    Epic 1 retrospective 2026-05-08).
    """

    event_type: ClassVar[str] = "agent_registry.agent_template.created"

    template_id: UUID
    name: str = Field(min_length=1, max_length=255)
    archetype: str = Field(min_length=1, max_length=50)
    version: int = Field(ge=1)
    actor: str = Field(default="system", description="user_id or 'system' for unattended creation")
    tenant_id: UUID | None = None


class AgentTemplateUpdatedEvent(BaseModel):
    """Published after an ``agent_templates`` row config (and possibly version) is committed.

    Story 2.2. Same audit-bypass story as :class:`AgentTemplateCreatedEvent`
    — lives in ``outbox_events`` until Story 9.1 wires :class:`AuditEventRepo`.

    ``old_version`` and ``new_version`` may be equal when the PUT payload
    did not include ``system_prompt`` (no prompt insert ⇒ no version bump).
    """

    event_type: ClassVar[str] = "agent_registry.agent_template.updated"

    template_id: UUID
    name: str = Field(min_length=1, max_length=255)
    archetype: str = Field(min_length=1, max_length=50)
    old_version: int = Field(ge=1)
    new_version: int = Field(ge=1)
    actor: str = Field(default="system", description="user_id or 'system' for unattended update")
    tenant_id: UUID | None = None


class AgentInstanceCreatedEvent(BaseModel):
    """Published after a new ``agent_instances`` row is durably committed (Story 2.4).

    Same audit-bypass story as the template events — lives in
    ``outbox_events`` until Story 9.1 wires :class:`AuditEventRepo`.
    The handler chain (Story 9.1+) will eventually persist this in
    ``audit_events`` via ``AuditEventRepo.record()``.

    The instance carries its own immutable ``snapshot`` of the template
    config at instantiation time — modifications to the template AFTER
    this event do not propagate to the running instance (FR12, AC2 of
    Story 2.4).
    """

    event_type: ClassVar[str] = "agent_registry.agent_instance.created"

    instance_id: UUID
    template_id: UUID
    template_version: int = Field(ge=1)
    workflow_run_id: UUID | None = None
    actor: str = Field(default="system", description="user_id or 'system' (D1 defer Story 9.1)")
    tenant_id: UUID | None = None


class AgentTemplateToolAssignedEvent(BaseModel):
    """Published when a tool is newly assigned to an agent_template (Story 2.5 AC2).

    Emitted by ``AgentRegistryService.replace_template_tools`` for each
    tool in the diff ``added`` set. Same audit-bypass pattern (TODO Story
    9.1 cleanup).
    """

    event_type: ClassVar[str] = "agent_registry.agent_template.tool_assigned"

    template_id: UUID
    tool_id: UUID
    tool_name: str = Field(min_length=1)
    actor: str = Field(default="system", description="user_id or 'system' (D1 defer Story 9.1)")
    tenant_id: UUID | None = None


class AgentTemplateToolUnassignedEvent(BaseModel):
    """Published when a tool is removed from an agent_template (Story 2.5 AC2 + AC3).

    Emitted (a) by ``AgentRegistryService.replace_template_tools`` for each
    tool in the diff ``removed`` set, OR (b) by
    ``AgentRegistryService.unassign_tool`` for the granular DELETE
    endpoint.
    """

    event_type: ClassVar[str] = "agent_registry.agent_template.tool_unassigned"

    template_id: UUID
    tool_id: UUID
    actor: str = Field(default="system", description="user_id or 'system' (D1 defer Story 9.1)")
    tenant_id: UUID | None = None


__all__ = [
    "AgentInstanceCreatedEvent",
    "AgentTemplateCreatedEvent",
    "AgentTemplateToolAssignedEvent",
    "AgentTemplateToolUnassignedEvent",
    "AgentTemplateUpdatedEvent",
]
