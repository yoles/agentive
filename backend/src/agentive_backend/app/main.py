"""FastAPI application factory + CLI entry point.

Usage :
    uv run agentive-backend       # via pyproject.toml [project.scripts]
    uv run uvicorn agentive_backend.app.main:app --reload   # direct uvicorn
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from agentive_backend import __version__
from agentive_backend.app.lifespan import lifespan
from agentive_backend.app.middleware import CorrelationIdMiddleware
from agentive_backend.infra.db.session import get_session_factory
from agentive_backend.shared.config import settings
from agentive_backend.shared.correlation import get_correlation_id
from agentive_backend.shared.exceptions import AgentiveError

log = structlog.get_logger(__name__)


def create_app() -> FastAPI:
    """Application factory — returns a fully configured FastAPI instance."""
    app = FastAPI(
        title="Agentive API",
        version=__version__,
        description="Company Builder for AI agents — internal API",
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs" if settings.is_development else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # ─── CORS ───
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Correlation-ID"],
    )

    # ─── Correlation ID ───
    app.add_middleware(CorrelationIdMiddleware)

    # ─── Exception handling (RFC 7807) ───
    @app.exception_handler(AgentiveError)
    async def handle_agentive_error(request: Request, exc: AgentiveError) -> JSONResponse:
        """Convert AgentiveError to RFC 7807 Problem Details response."""
        body: dict[str, Any] = {
            "type": exc.type,
            "title": exc.title,
            "status": exc.status,
            "correlation_id": get_correlation_id(),
        }
        if exc.detail:
            body["detail"] = exc.detail
        if exc.context:
            body["context"] = exc.context
        return JSONResponse(
            status_code=exc.status,
            content=body,
            media_type="application/problem+json",
        )

    # ─── Health + Ready ───
    @app.get("/health", tags=["infra"])
    async def health() -> dict[str, str]:
        """Liveness probe — returns OK if the process is running."""
        return {"status": "ok", "version": __version__}

    @app.get("/ready", tags=["infra"])
    async def ready() -> JSONResponse:
        """Readiness probe — verifies DB connectivity."""
        factory = get_session_factory()
        try:
            async with factory() as session:  # type: AsyncSession
                await session.execute(text("SELECT 1"))
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"status": "ready", "checks": {"db": "ok"}},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("readiness_check_failed", error=str(exc))
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "checks": {"db": f"error: {exc.__class__.__name__}"}},
            )

    # ─── API versioned router (empty Sprint 0 — features plug in Sprint 1+) ───
    # from agentive_backend.features.m2_agent_registry import router as agents_router
    # app.include_router(agents_router, prefix="/api/v1")

    return app


app = create_app()


def serve() -> None:
    """CLI entry — `agentive-backend` starts uvicorn with the app."""
    import uvicorn

    uvicorn.run(
        "agentive_backend.app.main:app",
        host="0.0.0.0",  # noqa: S104 (container runs inside Docker network)
        port=8000,
        reload=settings.is_development,
        access_log=False,  # structlog handles request logging via middleware
    )


if __name__ == "__main__":
    serve()
