"""Agent Registry — Epic M2 (Story 2.1+).

Public surface :
* :func:`load_registry` — load the 8 universal archetypes from YAML at lifespan.
* :class:`ArchetypeDefinition` — Pydantic v2 model of one archetype entry.
* :data:`router` — FastAPI router exposing ``/api/v1/agents/*`` endpoints.
* Focused services (audit A-10 split of the former ``AgentRegistryService``):
  :class:`ArchetypeCatalog`, :class:`AgentTemplateService`,
  :class:`TemplateInstantiationService`, :class:`TemplateToolAssignmentService`.
"""

from __future__ import annotations

from agentive_backend.features.agent_registry.archetypes import (
    ArchetypeDefinition,
    load_registry,
)
from agentive_backend.features.agent_registry.router import router
from agentive_backend.features.agent_registry.service import (
    AgentTemplateService,
    ArchetypeCatalog,
    TemplateInstantiationService,
    TemplateToolAssignmentService,
)

__all__ = [
    "AgentTemplateService",
    "ArchetypeCatalog",
    "ArchetypeDefinition",
    "TemplateInstantiationService",
    "TemplateToolAssignmentService",
    "load_registry",
    "router",
]
