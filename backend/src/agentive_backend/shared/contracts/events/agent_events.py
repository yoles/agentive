"""Agent lifecycle events — Epic M2 Agent Registry (Story 2.1+).

Naming convention follows Story 1.7 pattern ``{module}.{object}.{verb}``.
Initial events shipped Story 2.1 :

* ``m2.agent_template.created`` — emitted after a new ``agent_templates``
  row is committed via ``AgentRegistryService.create_template``.

Story 2.2 will add ``m2.agent_template.updated`` (config edit, prompt
versioning) and Story 2.4 will add ``m2.agent_instance.created`` /
``m2.agent_instance.completed``.

⚠️ Story 2.1 §"Pièges connus" #5 — the object is ``agent_template``, not
``agent``. Template ↔ instance distinction lands Story 2.4.
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

    event_type: ClassVar[str] = "m2.agent_template.created"

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

    event_type: ClassVar[str] = "m2.agent_template.updated"

    template_id: UUID
    name: str = Field(min_length=1, max_length=255)
    archetype: str = Field(min_length=1, max_length=50)
    old_version: int = Field(ge=1)
    new_version: int = Field(ge=1)
    actor: str = Field(default="system", description="user_id or 'system' for unattended update")
    tenant_id: UUID | None = None


__all__ = ["AgentTemplateCreatedEvent", "AgentTemplateUpdatedEvent"]
