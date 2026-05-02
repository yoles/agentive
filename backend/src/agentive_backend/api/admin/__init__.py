"""Admin endpoints — operational introspection (token rotation, health)."""

from __future__ import annotations

from agentive_backend.api.admin.health_llm import router as llm_health_router

__all__ = ["llm_health_router"]
