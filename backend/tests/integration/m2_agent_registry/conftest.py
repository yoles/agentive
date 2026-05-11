"""Integration fixtures for ``features.m2_agent_registry`` tests — Story 2.1.

Reuses Postgres + Alembic + role-provisioning machinery from the
``repositories`` package. ``clean_repository_tables`` is intentionally NOT
imported here — it's ``autouse=True`` in the repos conftest and would force
DB setup for every test in this directory, including the registry-only
lifespan test that doesn't need Postgres at all (same pattern as
``tests/integration/auth/conftest.py``).

Tests that DO need DB cleanup explicitly import the fixture and apply it
per-test (none currently — the e2e tests use unique names per test).

P-09 (CR 2026-05-10 Story 2.4) — ``make_e2e_app`` factor le builder FastAPI
test (~55 lignes dupliquées entre ``test_instantiate_template_e2e.py`` et
``test_workflow_run_instances_e2e.py``). Stories suivantes Epic 2/4 qui
ajouteront des e2e tests m2_agent_registry consommeront ce helper.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.app.middleware import AuthTokenMiddleware, CorrelationIdMiddleware
from agentive_backend.features.m2_agent_registry import load_registry
from agentive_backend.features.m2_agent_registry import router as agents_router
from agentive_backend.features.m5_tool_hub import router as tools_router
from agentive_backend.features.m7_playground import router as playground_router
from agentive_backend.shared.config import settings as _runtime_settings
from agentive_backend.shared.correlation import get_correlation_id
from agentive_backend.shared.exceptions import AgentiveError

# Reuse the repositories Postgres bootstrap (testcontainer + roles + alembic).
from tests.integration.repositories.conftest import (  # noqa: F401
    app_session_factory,
    audit_admin_session_factory,
    migrated_db,
    owner_session_factory,
    roles_provisioned,
    seed_session_factory,
)

E2E_AUTH_TOKEN = "integration-test-token"


def make_e2e_app(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    token: str = E2E_AUTH_TOKEN,
) -> FastAPI:
    """Build a minimal FastAPI app exercising the full m2_agent_registry stack.

    Mirrors the production ``app.main`` wiring : ``AuthTokenMiddleware`` +
    ``CorrelationIdMiddleware`` + the agents router under ``/api/v1`` + the
    RFC 7807 exception handlers (``AgentiveError`` and Pydantic
    ``RequestValidationError``).

    Sprint 1 — does NOT install a generic ``Exception`` handler. An
    unhandled exception (e.g. monkeypatched ``publish`` raising
    ``RuntimeError`` in the AC5 atomicity test) propagates through the
    httpx ASGI transport ; tests using such a setup must wrap the call
    in ``pytest.raises``.
    """
    app = FastAPI()
    app.state.auth_token_hash = token
    app.state.session_factory = session_factory
    app.state.archetype_registry = load_registry()

    app.add_middleware(AuthTokenMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(agents_router, prefix="/api/v1")
    # Story 2.5 — m5 tool_hub router included as well so e2e tests can
    # cross-feature exercise tool registration → tool assignment → list.
    app.include_router(tools_router, prefix="/api/v1")
    # Story 2.7 — m7 playground router (cross-feature : Playground tests
    # need template + tools + LLM router wired in app.state).
    app.include_router(playground_router, prefix="/api/v1")

    @app.exception_handler(AgentiveError)
    async def _handle_agentive_error(  # pragma: no cover — verbatim of app.main handler
        _request: Request, exc: AgentiveError
    ) -> JSONResponse:
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
            for k, v in exc.context.items():
                if k not in reserved:
                    body[k] = v
        return JSONResponse(
            status_code=exc.status, content=body, media_type="application/problem+json"
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_req_validation(  # pragma: no cover — verbatim of app.main handler
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        sanitized_errors = [
            {k: v for k, v in err.items() if k not in {"input", "ctx"}} for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "type": "/errors/validation",
                "title": "Validation failed",
                "status": status.HTTP_422_UNPROCESSABLE_ENTITY,
                "correlation_id": get_correlation_id(),
                "detail": "Request body validation failed",
                "errors": sanitized_errors,
            },
            media_type="application/problem+json",
        )

    return app


def e2e_auth_headers() -> dict[str, str]:
    """Standard `Authorization: Bearer …` headers for e2e tests."""
    return {"Authorization": f"Bearer {E2E_AUTH_TOKEN}"}


@pytest.fixture(autouse=True)
def _enable_mcp_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Story 2.5 P-23 admin-gate — flip ``AGENTIVE_ALLOW_MCP_REGISTRATION``
    to True for the duration of m2/m5 e2e tests.

    Default is False (production-safe — RCE/SSRF until Story 2.6 sandbox).
    Tests need it True to exercise the POST /tools/servers happy/409/503
    paths. The 403 path test flips it back to False inline.

    ``monkeypatch.setattr`` on a Pydantic v2 BaseSettings instance is safe
    because BaseSettings is mutable by default and ``monkeypatch`` restores
    the original value at teardown.
    """
    monkeypatch.setattr(_runtime_settings, "mcp_allow_registration", True)
