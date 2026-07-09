"""Tool Hub — Epic M5.

Story 2.5 — Sprint 1 livre la **plomberie** : registry des serveurs MCP +
discovery des outils + assignment aux agent-templates. L'**exécution
runtime** des outils (call MCP réel via sandbox bwrap) arrive Story 2.6.

Public surface :
    - :data:`router` — FastAPI APIRouter for ``/api/v1/tools/*``.
    - :class:`ToolHubService` — orchestrate MCP discovery + persistence.
"""

from agentive_backend.features.tool_hub.router import router
from agentive_backend.features.tool_hub.service import ToolHubService

__all__ = ["ToolHubService", "router"]
