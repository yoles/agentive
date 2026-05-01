"""Event schemas — published on the event bus.

Naming convention : ``module.entity.action``
    - ``system.app.started``, ``system.app.shutdown``, ``system.health.checked``
    - ``m2.agent.created`` (Epic 2)
    - ``m3.workflow.started``, ``m3.workflow.completed`` (Epic 4)
    - ``m4.chunk.indexed``, ``m4.chunk.archived`` (Epic 3)

System events are published by :mod:`app.lifespan` at boot/shutdown and by
:func:`app.main.create_app`'s ``/ready`` endpoint after each readiness probe.
Module-specific event modules are stubbed and filled by their owning epics.
"""

from __future__ import annotations

from agentive_backend.shared.contracts.events.health_events import HealthCheckEvent
from agentive_backend.shared.contracts.events.system_events import (
    SystemShutdownEvent,
    SystemStartedEvent,
)

__all__ = [
    "HealthCheckEvent",
    "SystemShutdownEvent",
    "SystemStartedEvent",
]
