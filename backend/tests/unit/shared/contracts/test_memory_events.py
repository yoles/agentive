"""Unit tests for ``shared/contracts/events/memory_events.py`` — Story 3.2 T4."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentive_backend.shared.contracts.events import (
    MemoryChunkArchivedEvent,
    NamespaceAccessDeniedEvent,
    NamespaceCreatedEvent,
)


def test_namespace_created_event_type_constant() -> None:
    assert NamespaceCreatedEvent.event_type == "memory_manager.namespace.created"


def test_namespace_created_serializes_with_required_fields() -> None:
    event = NamespaceCreatedEvent(
        namespace_id=uuid4(),
        name="dev-notes",
        namespace_type="metier",
        department="Dev",
        project=None,
    )
    dumped = event.model_dump()
    assert dumped["name"] == "dev-notes"
    assert dumped["namespace_type"] == "metier"
    assert dumped["department"] == "Dev"
    assert dumped["actor"] == "system"  # D1 default
    assert dumped["tenant_id"] is None


def test_namespace_access_denied_event_type_constant() -> None:
    assert NamespaceAccessDeniedEvent.event_type == "memory_manager.namespace.access_denied"


def test_namespace_access_denied_serializes_with_required_fields() -> None:
    event = NamespaceAccessDeniedEvent(
        namespace_id=uuid4(),
        namespace="dev-notes",
        namespace_department="Dev",
        acting_department="Design-UX",
        operation="search",
    )
    dumped = event.model_dump()
    assert dumped["namespace"] == "dev-notes"
    assert dumped["namespace_department"] == "Dev"
    assert dumped["acting_department"] == "Design-UX"
    assert dumped["operation"] == "search"
    assert dumped["actor"] == "system"


def test_namespace_access_denied_rejects_invalid_operation() -> None:
    with pytest.raises(ValidationError):
        NamespaceAccessDeniedEvent(
            namespace_id=uuid4(),
            namespace="dev-notes",
            namespace_department="Dev",
            acting_department="Design-UX",
            operation="delete",  # type: ignore[arg-type]  # NOT in Literal
        )


# ─── MemoryChunkArchivedEvent (Story 3.3 T3.1) ─────────────────────


def test_memory_chunk_archived_event_type_constant() -> None:
    assert MemoryChunkArchivedEvent.event_type == "memory_manager.chunk.archived"


def test_memory_chunk_archived_serializes_with_required_fields() -> None:
    event = MemoryChunkArchivedEvent(
        chunk_id=uuid4(),
        namespace_id=uuid4(),
        namespace="dev-notes",
        reason="ttl_expired",
    )
    dumped = event.model_dump()
    assert dumped["namespace"] == "dev-notes"
    assert dumped["reason"] == "ttl_expired"
    assert dumped["actor"] == "system"  # D1 default
    assert dumped["tenant_id"] is None


def test_memory_chunk_archived_accepts_archive_after_seconds_reason() -> None:
    """AC3's secondary path — kept a `Literal`, not a `bool`, so a future
    story (3.6 manual purge) can add a third reason without a breaking
    change (this story's Dev Notes § Project Context Reference)."""
    event = MemoryChunkArchivedEvent(
        chunk_id=uuid4(),
        namespace_id=uuid4(),
        namespace="dev-notes",
        reason="archive_after_seconds",
    )
    assert event.reason == "archive_after_seconds"


def test_memory_chunk_archived_accepts_manual_purge_reason() -> None:
    """Story 3.6 AC1 — `MemoryManagerService.purge_chunk` reuses this event
    with a third `reason`, exactly as this event's own docstring
    anticipated before Story 3.6 existed."""
    event = MemoryChunkArchivedEvent(
        chunk_id=uuid4(),
        namespace_id=uuid4(),
        namespace="dev-notes",
        reason="manual_purge",
    )
    assert event.reason == "manual_purge"


def test_memory_chunk_archived_rejects_invalid_reason() -> None:
    with pytest.raises(ValidationError):
        MemoryChunkArchivedEvent(
            chunk_id=uuid4(),
            namespace_id=uuid4(),
            namespace="dev-notes",
            reason="bogus_reason",  # type: ignore[arg-type]
        )
