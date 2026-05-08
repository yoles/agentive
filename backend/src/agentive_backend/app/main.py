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

from agentive_backend import __version__
from agentive_backend.app.lifespan import lifespan
from agentive_backend.app.middleware import AuthTokenMiddleware, CorrelationIdMiddleware
from agentive_backend.infra.db.session import get_session_factory
from agentive_backend.shared.config import settings
from agentive_backend.shared.contracts.events import HealthCheckEvent
from agentive_backend.shared.correlation import get_correlation_id
from agentive_backend.shared.event_bus import publish_and_commit
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
    # Explicit allowlists — CORS spec forbids wildcards with `allow_credentials=True`.
    # Origins are configurable via `AGENTIVE_CORS_ALLOW_ORIGINS` (JSON list).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Correlation-ID",
            "Accept",
            "Accept-Language",
        ],
        expose_headers=["X-Correlation-ID"],
    )

    # ─── Middleware stack (LIFO execution order in Starlette) ───────────────
    # Starlette executes middleware in reverse registration order.
    # To ensure CorrelationIdMiddleware runs BEFORE AuthTokenMiddleware
    # (so the correlation_id ContextVar is bound before the 401 is built),
    # AuthTokenMiddleware must be registered FIRST.
    app.add_middleware(AuthTokenMiddleware)  # registered first → runs SECOND
    app.add_middleware(CorrelationIdMiddleware)  # registered second → runs FIRST

    # ─── Exception handling (RFC 7807) ───
    @app.exception_handler(AgentiveError)
    async def handle_agentive_error(_request: Request, exc: AgentiveError) -> JSONResponse:
        """Convert AgentiveError to RFC 7807 Problem Details response.

        Body shape (RFC 7807 §3 — Extension Members at top-level, NOT nested) :

        - Standard fields ``type`` / ``title`` / ``status`` / ``detail`` / ``correlation_id``
          are written first.
        - ``exc.context`` (extension members like ``agent_id``, ``module``, ``tenant_id``)
          is then merged at the top-level. RFC 7807 standard fields take precedence —
          colliding context keys are dropped and a structured WARNING is emitted with
          the list of collisions, so callers can detect malicious or buggy ``raise``
          sites without affecting the response shape.
        """
        body: dict[str, Any] = {
            "type": exc.type,
            "title": exc.title,
            "status": exc.status,
            "correlation_id": get_correlation_id(),
        }
        if exc.detail:
            body["detail"] = exc.detail
        if exc.context:
            reserved = {"type", "title", "status", "correlation_id", "detail"}
            colliding = sorted(set(exc.context) & reserved)
            if colliding:
                log.warning(
                    "rfc7807_context_collision",
                    colliding_keys=colliding,
                    exception_type=type(exc).__name__,
                )
            for k, v in exc.context.items():
                if k not in reserved:
                    body[k] = v
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
        """Readiness probe — verifies DB connectivity.

        On failure the response body exposes only a constant ``"error"`` label
        to avoid leaking implementation details (exception class names, driver
        errors) to unauthenticated callers. Full diagnostics are logged
        server-side with correlation ID.

        On the **success path only** (P5) we best-effort publish a
        :class:`HealthCheckEvent` so downstream observers (Trace Explorer M12,
        Dashboard M6) can build liveness timelines. We deliberately skip the
        publish on the failure path to avoid amplifying DB outage load with a
        second session+INSERT against the same broken database (high-frequency
        liveness probes would otherwise compound the incident).
        """
        factory = get_session_factory()
        try:
            async with factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:  # readiness must swallow all failures
            log.warning(
                "readiness_check_failed",
                error_class=exc.__class__.__name__,
                error=str(exc),
            )
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "checks": {"db": "error"}},
            )

        response_body = {"status": "ready", "checks": {"db": "ok"}}
        # Best-effort event publish — success path only (P5).
        try:
            async with factory() as session:
                await publish_and_commit(
                    session,
                    HealthCheckEvent.event_type,
                    HealthCheckEvent(
                        status=response_body["status"],
                        checks=response_body["checks"],
                    ),
                )
        except Exception:
            log.warning("ready_event_publish_failed")

        return JSONResponse(status_code=status.HTTP_200_OK, content=response_body)

    # ─── RFC 7807 422 for Pydantic body validation errors ─────────────────
    # FastAPI's default 422 returns ``{"detail": [...]}`` — not RFC 7807. We
    # override here so all 422 responses (Pydantic + AgentiveError.ValidationError)
    # share the same ``application/problem+json`` shape (Story 2.1 AC3).
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Story 2.1 P-06 — strip `input` from each error entry so a caller
        # who accidentally posts a credential / PII into a typed field
        # doesn't see it echoed back in the 422 response. The structured
        # type/loc/msg/ctx are sufficient for client-side error rendering.
        # `ctx` may also embed user input on certain validators; we drop it
        # for the same reason. Operators still see the full raw error
        # server-side via the structlog WARNING below.
        sanitized_errors = [
            {k: v for k, v in err.items() if k not in {"input", "ctx"}} for err in exc.errors()
        ]
        log.warning(
            "request_validation_failed",
            error_count=len(sanitized_errors),
            error_types=[err.get("type") for err in sanitized_errors],
        )
        body: dict[str, Any] = {
            "type": "/errors/validation",
            "title": "Validation failed",
            "status": status.HTTP_422_UNPROCESSABLE_ENTITY,
            "correlation_id": get_correlation_id(),
            "detail": "Request body validation failed",
            "errors": sanitized_errors,
        }
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=body,
            media_type="application/problem+json",
        )

    # ─── Admin endpoints (Sprint 0 — operational introspection) ───
    from agentive_backend.api.admin import llm_health_router, rotate_token_router

    app.include_router(llm_health_router, prefix="/api/v1/admin")
    app.include_router(rotate_token_router, prefix="/api/v1/admin")

    # ─── API versioned router — Story 2.1+ ────────────────────────────────
    from agentive_backend.features.m2_agent_registry import router as agents_router

    app.include_router(agents_router, prefix="/api/v1")

    return app


app = create_app()


def serve() -> None:
    """CLI entry — `agentive-backend` starts uvicorn with the app."""
    import uvicorn

    uvicorn.run(
        "agentive_backend.app.main:app",
        host="0.0.0.0",  # container runs inside Docker network
        port=8000,
        reload=settings.is_development,
        access_log=False,  # structlog handles request logging via middleware
    )


if __name__ == "__main__":
    serve()
