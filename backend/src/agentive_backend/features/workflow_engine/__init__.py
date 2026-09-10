"""Workflow Engine — Epic 4 (Workflow Orchestration Engine).

Story 4.1 — ``POST /api/v1/workflows`` : DAG validation + versioned
persistence + non-blocking Contrôleur/Producteur LLM-diversity alert (FR15,
D84).

Public surface:
    - :data:`router` — FastAPI APIRouter for ``/api/v1/workflows``.
    - :class:`WorkflowService` — DAG validation + versioned persistence.
"""

from __future__ import annotations

from agentive_backend.features.workflow_engine.router import router
from agentive_backend.features.workflow_engine.service import WorkflowService

__all__ = ["WorkflowService", "router"]
