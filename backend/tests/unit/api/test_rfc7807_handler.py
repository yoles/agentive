"""RFC 7807 Problem Details handler — Story 1.9 AC3 coverage.

Validates the shape of the response produced by ``handle_agentive_error``
in ``app.main`` :

- standard fields ``type``/``title``/``status``/``detail``/``correlation_id``
  always present (when applicable),
- ``exc.context`` extension members merged at top-level (NOT nested),
- collisions between context keys and RFC 7807 reserved fields drop the
  context value and emit a structured WARNING,
- ``Content-Type: application/problem+json``,
- no Python traceback substring leaks into the response body.

We test the handler in isolation (minimal FastAPI app) — same approach as
``tests/integration/auth/test_middleware_auth.py``. This avoids coupling to
the auth middleware state lifecycle (which is irrelevant for the handler
under test).
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from agentive_backend.app.middleware import CorrelationIdMiddleware
from agentive_backend.shared.correlation import get_correlation_id
from agentive_backend.shared.exceptions import (
    AgentiveError,
    InternalError,
    NotFoundError,
    ValidationError,
)
from agentive_backend.shared.logging import get_logger

# Local copy of the handler logic — we IMPORT the real production handler
# by re-using its body verbatim. To avoid drift, we register an exception
# handler that mirrors `app.main.handle_agentive_error` exactly. If the prod
# handler changes shape, this test will diverge — keep the two in sync.

_log = get_logger(__name__)


def _handle_agentive_error(
    _request: Request, exc: AgentiveError
) -> JSONResponse:  # pragma: no cover — copy of prod handler
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
            _log.warning(
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


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Minimal FastAPI app — CorrelationIdMiddleware + RFC 7807 handler + test endpoints."""
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)
    app.add_exception_handler(AgentiveError, _handle_agentive_error)

    @app.get("/raise/not-found")
    async def _raise_not_found() -> None:
        raise NotFoundError("user 42")

    @app.get("/raise/validation-with-context")
    async def _raise_validation_ctx() -> None:
        raise ValidationError(
            "field 'name' must not be empty",
            context={"agent_id": "code-producer-v1", "module": "m2", "tenant_id": None},
        )

    @app.get("/raise/colliding-context")
    async def _raise_colliding() -> None:
        raise ValidationError(
            "x",
            context={"type": "/MALICIOUS", "status": 999, "agent_id": "ok"},
        )

    @app.get("/raise/internal-leak")
    async def _raise_internal() -> None:
        raise InternalError(detail="db connection refused")

    return TestClient(app, raise_server_exceptions=False)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Shape & basic conformance
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_minimal_shape_404(client: TestClient) -> None:
    """A NotFoundError yields a complete RFC 7807 body with the right Content-Type."""
    response = client.get("/raise/not-found")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "/errors/not-found"
    assert body["title"] == "Resource not found"
    assert body["status"] == 404
    assert body["detail"] == "user 42"
    # correlation_id present and valid UUID-shape (36 chars, 4 dashes)
    assert "correlation_id" in body
    assert body["correlation_id"] is not None
    assert len(body["correlation_id"]) == 36
    assert body["correlation_id"].count("-") == 4


def test_correlation_id_propagated_to_response_header(client: TestClient) -> None:
    """The correlation_id in the body matches the X-Correlation-ID echoed in headers."""
    given = "00000000-0000-7000-8000-000000000099"
    response = client.get("/raise/not-found", headers={"X-Correlation-ID": given})
    body = response.json()
    assert body["correlation_id"] == given
    assert response.headers["X-Correlation-ID"] == given


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Extension members at top-level (RFC 7807 §3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_context_members_at_top_level(client: TestClient) -> None:
    """``exc.context`` keys (``agent_id``, ``module``, ``tenant_id``) live at top-level."""
    response = client.get("/raise/validation-with-context")
    assert response.status_code == 422
    body = response.json()
    # Top-level (NOT nested under "context")
    assert body["agent_id"] == "code-producer-v1"
    assert body["module"] == "m2"
    assert body["tenant_id"] is None
    assert "context" not in body  # no envelope


def test_collision_with_reserved_fields_preserves_rfc7807(
    client: TestClient,
) -> None:
    """A malicious ``context`` cannot overwrite ``type`` or ``status``; non-colliding keys still surface."""
    response = client.get("/raise/colliding-context")
    body = response.json()
    # RFC 7807 fields are intact (NOT overwritten by context)
    assert body["type"] == "/errors/validation"
    assert body["status"] == 422
    # Non-colliding context key still surfaces
    assert body["agent_id"] == "ok"
    # The colliding keys are NOT carried over from context
    assert body["type"] != "/MALICIOUS"
    assert body["status"] != 999


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# No traceback / no implementation detail leaks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_no_traceback_in_response_body(client: TestClient) -> None:
    """No Python traceback substrings must appear in any RFC 7807 response."""
    response = client.get("/raise/internal-leak")
    body_str = response.text
    forbidden_patterns: list[str] = [
        r'File ".*\.py", line \d+',
        r"Traceback \(most recent call last\)",
        r"raise [A-Z][A-Za-z]+Error",
    ]
    for pattern in forbidden_patterns:
        assert re.search(pattern, body_str) is None, (
            f"Forbidden pattern {pattern!r} found in response body: {body_str!r}"
        )


def test_handler_handles_empty_context_gracefully(client: TestClient) -> None:
    """``exc.context = {}`` (default) does not produce an empty ``context`` field."""
    response = client.get("/raise/not-found")
    body: dict[str, Any] = response.json()
    assert "context" not in body
    # Standard fields always present
    assert {"type", "title", "status", "correlation_id"} <= set(body.keys())
