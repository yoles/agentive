"""Memory Manager — Epic M4 (Story 3.1+).

Story 3.1 — chunk storage + vector search (pgvector HNSW). Isolation
(RLS département), archivage, decay temporel, Push Memory et purge/routing
d'embedding arrivent Stories 3.2-3.6.

Public surface:
    - :data:`router` — FastAPI APIRouter for ``/api/v1/memory/*``.
    - :class:`MemoryManagerService` — orchestrate chunk storage + ANN search.
"""

from __future__ import annotations

from agentive_backend.features.memory_manager.router import router
from agentive_backend.features.memory_manager.service import MemoryManagerService

__all__ = ["MemoryManagerService", "router"]
