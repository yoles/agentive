"""Unit tests — agent lifecycle events (Story 2.4 T3.3 + Story 2.1/2.2 baseline)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentive_backend.shared.contracts.events import (
    AgentInstanceCreatedEvent,
    AgentTemplateCreatedEvent,
    AgentTemplateUpdatedEvent,
)


def test_agent_template_created_event_type_constant() -> None:
    assert AgentTemplateCreatedEvent.event_type == "m2.agent_template.created"


def test_agent_template_updated_event_type_constant() -> None:
    assert AgentTemplateUpdatedEvent.event_type == "m2.agent_template.updated"


def test_agent_instance_created_event_type_constant() -> None:
    """Story 2.4 — new audit event for instance creation."""
    assert AgentInstanceCreatedEvent.event_type == "m2.agent_instance.created"


def test_agent_instance_created_event_validates_minimal_payload() -> None:
    event = AgentInstanceCreatedEvent(
        instance_id=uuid4(),
        template_id=uuid4(),
        template_version=1,
    )
    assert event.actor == "system"
    assert event.workflow_run_id is None
    assert event.tenant_id is None


def test_agent_instance_created_event_rejects_zero_template_version() -> None:
    """`template_version` must be >= 1 (cohérent avec AgentTemplate.version server_default=1)."""
    with pytest.raises(ValidationError):
        AgentInstanceCreatedEvent(
            instance_id=uuid4(),
            template_id=uuid4(),
            template_version=0,
        )


def test_agent_instance_created_event_accepts_workflow_run_id() -> None:
    run_id = uuid4()
    event = AgentInstanceCreatedEvent(
        instance_id=uuid4(),
        template_id=uuid4(),
        template_version=3,
        workflow_run_id=run_id,
    )
    assert event.workflow_run_id == run_id
