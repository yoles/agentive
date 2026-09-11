"""Event schemas — published on the event bus.

Naming convention : ``module.object.verb`` (Story 1.7 pattern, e.g.
``system.token.used``).
    - ``system.app.started``, ``system.app.shutdown``, ``system.health.checked``
    - ``agent_registry.agent_template.created`` (Story 2.1) — Story 2.4 will add
      ``agent_registry.agent_instance.*``.
    - ``workflow_engine.workflow.started``, ``workflow_engine.workflow.completed`` (Epic 4)
    - ``memory_manager.chunk.indexed``, ``memory_manager.chunk.archived`` (Epic 3)

System events are published by :mod:`app.lifespan` at boot/shutdown and by
:func:`app.main.create_app`'s ``/ready`` endpoint after each readiness probe.
Module-specific event modules are filled by their owning epics.
"""

from __future__ import annotations

from agentive_backend.shared.contracts.events.agent_events import (
    AgentInstanceCreatedEvent,
    AgentTemplateCreatedEvent,
    AgentTemplateToolAssignedEvent,
    AgentTemplateToolUnassignedEvent,
    AgentTemplateUpdatedEvent,
)
from agentive_backend.shared.contracts.events.health_events import HealthCheckEvent
from agentive_backend.shared.contracts.events.memory_events import (
    MemoryChunkArchivedEvent,
    NamespaceAccessDeniedEvent,
    NamespaceCreatedEvent,
    NamespaceDecayPolicyUpdatedEvent,
)
from agentive_backend.shared.contracts.events.playground_events import (
    PlaygroundRunCompletedEvent,
)
from agentive_backend.shared.contracts.events.system_events import (
    SystemShutdownEvent,
    SystemStartedEvent,
)
from agentive_backend.shared.contracts.events.tool_events import (
    ToolDiscoveredEvent,
    ToolInvokedEvent,
    ToolServerConnectedEvent,
)
from agentive_backend.shared.contracts.events.workflow_events import (
    WorkflowCreatedEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunFailedEvent,
    WorkflowRunResumedEvent,
    WorkflowRunRoutingEscalatedEvent,
    WorkflowRunStartedEvent,
    WorkflowRunStepCompletedEvent,
)

__all__ = [
    "AgentInstanceCreatedEvent",
    "AgentTemplateCreatedEvent",
    "AgentTemplateToolAssignedEvent",
    "AgentTemplateToolUnassignedEvent",
    "AgentTemplateUpdatedEvent",
    "HealthCheckEvent",
    "MemoryChunkArchivedEvent",
    "NamespaceAccessDeniedEvent",
    "NamespaceCreatedEvent",
    "NamespaceDecayPolicyUpdatedEvent",
    "PlaygroundRunCompletedEvent",
    "SystemShutdownEvent",
    "SystemStartedEvent",
    "ToolDiscoveredEvent",
    "ToolInvokedEvent",
    "ToolServerConnectedEvent",
    "WorkflowCreatedEvent",
    "WorkflowRunCompletedEvent",
    "WorkflowRunFailedEvent",
    "WorkflowRunResumedEvent",
    "WorkflowRunRoutingEscalatedEvent",
    "WorkflowRunStartedEvent",
    "WorkflowRunStepCompletedEvent",
]
