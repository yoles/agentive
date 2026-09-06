"""Memory manager events — Epic 3.

Naming convention follows the ``module.entity.action`` pattern (cf.
``shared/event_bus/naming.py``). Events shipped:

* ``memory_manager.namespace.created`` — Story 3.2, emitted after a
  namespace row is durably committed via ``POST /memory/namespaces``.
* ``memory_manager.namespace.access_denied`` — Story 3.2, emitted when a
  caller's declared ``X-Acting-Department`` differs from the target
  namespace's ``department`` (AC2).
* ``memory_manager.chunk.archived`` — Story 3.3, emitted after
  ``MemoryArchivalWorker`` durably sets a chunk's ``archived_at`` (AC1/AC3).
* ``memory_manager.namespace.decay_policy_updated`` — Story 3.4, emitted
  after ``PATCH /memory/namespaces/{name}`` durably changes a namespace's
  ``decay_policy`` (code review BS1).

Filled in by Story 3.2 — this file was a placeholder left by Story 3.1
("Filled by Epic 3 stories").
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class NamespaceCreatedEvent(BaseModel):
    """Published after a new ``namespaces`` row is durably committed
    (Story 3.2 AC1). Lives in ``outbox_events`` until Story 9.1 wires
    :class:`AuditEventRepo` (same audit-bypass posture as the rest of the
    codebase's typed events).
    """

    event_type: ClassVar[str] = "memory_manager.namespace.created"

    namespace_id: UUID
    name: str = Field(min_length=1, max_length=255)
    namespace_type: str
    department: str | None = None
    project: str | None = None
    actor: str = Field(default="system", description="user_id or 'system' (D1 defer Story 9.1)")
    tenant_id: UUID | None = None


class NamespaceAccessDeniedEvent(BaseModel):
    """Published when a request's ``X-Acting-Department`` differs from the
    target namespace's ``department`` (Story 3.2 AC2). Committed BEFORE the
    caller-facing ``ForbiddenError`` is raised — see
    ``MemoryManagerService._check_department_access`` for why the ordering
    matters (a rollback would silently drop this audit trail row).
    """

    event_type: ClassVar[str] = "memory_manager.namespace.access_denied"

    namespace_id: UUID
    namespace: str = Field(min_length=1, max_length=255)
    namespace_department: str
    acting_department: str
    operation: Literal["create_chunk", "search"]
    actor: str = Field(default="system", description="user_id or 'system' (D1 defer Story 9.1)")
    tenant_id: UUID | None = None


class MemoryChunkArchivedEvent(BaseModel):
    """Published after ``MemoryArchivalWorker`` durably archives a chunk
    (Story 3.3 AC1/AC3), in the same transaction as the ``archived_at``
    UPDATE (mirror atomicity ``MemoryManagerService.create_namespace``).

    ``reason`` is a ``Literal`` (not a ``bool``) on purpose — Story 3.6 will
    very likely reuse ``mark_archived``/this event for its manual-purge
    soft-delete, adding a third reason (cf. this story's Dev Notes §
    Project Context Reference); keeping it open avoids a breaking change
    later.
    """

    event_type: ClassVar[str] = "memory_manager.chunk.archived"

    chunk_id: UUID
    namespace_id: UUID
    namespace: str = Field(min_length=1, max_length=255)
    reason: Literal["ttl_expired", "archive_after_seconds"]
    actor: str = Field(default="system", description="user_id or 'system' (D1 defer Story 9.1)")
    tenant_id: UUID | None = None


class NamespaceDecayPolicyUpdatedEvent(BaseModel):
    """Published after a namespace's ``decay_policy`` is durably committed
    (Story 3.4, code review BS1). Lives in ``outbox_events`` until Story 9.1
    wires :class:`AuditEventRepo`, same posture as the events above.

    Carries the policy itself, unlike :class:`NamespaceCreatedEvent` which is
    identity-only: here the configuration IS the event. A decay policy
    silently reorders every future search on the namespace, so "who changed
    it to what" is the whole audit value — recording only that *something*
    changed would leave nothing to reconstruct.
    """

    event_type: ClassVar[str] = "memory_manager.namespace.decay_policy_updated"

    namespace_id: UUID
    name: str = Field(min_length=1, max_length=255)
    decay_policy: dict[str, Any]
    actor: str = Field(default="system", description="user_id or 'system' (D1 defer Story 9.1)")
    tenant_id: UUID | None = None


__all__ = [
    "MemoryChunkArchivedEvent",
    "NamespaceAccessDeniedEvent",
    "NamespaceCreatedEvent",
    "NamespaceDecayPolicyUpdatedEvent",
]
