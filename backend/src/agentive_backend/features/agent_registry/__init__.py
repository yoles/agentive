"""Agent Registry — Epic M2 (Story 2.1+).

Public surface :
* :func:`load_registry` — load the 8 universal archetypes from YAML at lifespan.
* :class:`ArchetypeDefinition` — Pydantic v2 model of one archetype entry.
* :data:`router` — FastAPI router exposing ``/api/v1/agents/*`` endpoints.
* :class:`AgentRegistryService` — orchestration on top of ``AgentTemplateRepo``.
"""

from __future__ import annotations

from agentive_backend.features.agent_registry.archetypes import (
    ArchetypeDefinition,
    load_registry,
)
from agentive_backend.features.agent_registry.router import router
from agentive_backend.features.agent_registry.service import AgentRegistryService

__all__ = [
    "AgentRegistryService",
    "ArchetypeDefinition",
    "load_registry",
    "router",
]
