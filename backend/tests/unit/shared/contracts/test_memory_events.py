"""Unit tests for ``shared/contracts/events/memory_events.py`` — Story 3.2 T4."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentive_backend.shared.contracts.events import (
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
