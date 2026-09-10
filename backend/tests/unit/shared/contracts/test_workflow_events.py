"""Unit tests — workflow lifecycle events (Story 4.1 T5)."""

from __future__ import annotations

from uuid import uuid4

from agentive_backend.shared.contracts.events import WorkflowCreatedEvent


def test_workflow_created_event_type_constant() -> None:
    assert WorkflowCreatedEvent.event_type == "workflow_engine.workflow.created"


def test_workflow_created_event_validates_minimal_payload() -> None:
    event = WorkflowCreatedEvent(
        workflow_id=uuid4(),
        name="ingest",
        version=1,
        node_count=2,
    )
    assert event.actor == "system"
    assert event.tenant_id is None
