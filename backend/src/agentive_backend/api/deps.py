"""FastAPI dependency providers — wired in app.main."""

from __future__ import annotations

from fastapi import Request

from agentive_backend.shared.llm import LLMRouter


def get_llm_router(request: Request) -> LLMRouter:
    """Return the singleton :class:`LLMRouter` built in ``app.lifespan``."""
    router = getattr(request.app.state, "llm_router", None)
    if router is None:
        # Lifespan failed to wire the router — surface loudly so callers
        # do not see a confusing AttributeError.
        raise RuntimeError("app.state.llm_router not configured — was the lifespan called?")
    return router  # type: ignore[no-any-return]
