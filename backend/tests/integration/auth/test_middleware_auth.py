"""Integration tests for AuthTokenMiddleware — Story 1.7 (AC2)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentive_backend.app.middleware import AuthTokenMiddleware, CorrelationIdMiddleware


def _make_app(token: str = "change_me") -> FastAPI:
    """Minimal FastAPI app with auth middleware (no full lifespan)."""
    app = FastAPI()
    app.state.auth_token_hash = token
    # Minimal session_factory stub — auth middleware only needs it for the
    # fire-and-forget audit task, which swallows errors.
    app.state.session_factory = None

    # Starlette LIFO — AuthToken registered first so CorrelationId runs first.
    app.add_middleware(AuthTokenMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/api/v1/test")
    async def _test_endpoint() -> dict[str, str]:
        return {"ok": "true"}

    return app


# ─── 401 on missing Authorization header ──────────────────────────────────────


def test_missing_auth_header_returns_401() -> None:
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/api/v1/test")
    assert response.status_code == 401
    body = response.json()
    assert body["type"] == "/errors/auth/missing-token"
    assert body["status"] == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers.get("www-authenticate") == "Bearer"


def test_missing_auth_header_includes_correlation_id() -> None:
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/api/v1/test")
    assert "correlation_id" in response.json()
    assert response.json()["correlation_id"] is not None


# ─── 401 on wrong token ────────────────────────────────────────────────────────


def test_wrong_token_returns_401() -> None:
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/api/v1/test", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401
    body = response.json()
    assert body["type"] == "/errors/auth/invalid-token"
    assert response.headers["content-type"] == "application/problem+json"


# ─── 200 on correct token ─────────────────────────────────────────────────────


def test_correct_token_passes_through() -> None:
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/api/v1/test", headers={"Authorization": "Bearer change_me"})
    assert response.status_code == 200
    assert response.json() == {"ok": "true"}


# ─── Public routes bypass auth ────────────────────────────────────────────────


def test_health_route_bypasses_auth() -> None:
    app = _make_app()

    @app.get("/health")
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/health")  # no Authorization header
    assert response.status_code == 200


def test_ready_route_bypasses_auth() -> None:
    app = _make_app()

    @app.get("/ready")
    async def _ready() -> dict[str, str]:
        return {"status": "ready"}

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/ready")
    assert response.status_code == 200


# ─── X-Correlation-ID header is present in 401 response ─────────────────────


def test_correlation_id_header_in_401() -> None:
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/api/v1/test")
    assert "x-correlation-id" in response.headers


# ─── P7 — Bearer scheme is case-insensitive (RFC 7235) ───────────────────────


def test_lowercase_bearer_scheme_accepted() -> None:
    """`bearer` (lowercase) is RFC 7235-valid and must authenticate."""
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/api/v1/test", headers={"Authorization": "bearer change_me"})
    assert response.status_code == 200


def test_uppercase_bearer_scheme_accepted() -> None:
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/api/v1/test", headers={"Authorization": "BEARER change_me"})
    assert response.status_code == 200


# ─── P11 — Empty Bearer token returns 401 missing-token, not invalid-token ───


def test_empty_bearer_token_returns_missing_token() -> None:
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/api/v1/test", headers={"Authorization": "Bearer "})
    assert response.status_code == 401
    assert response.json()["type"] == "/errors/auth/missing-token"


def test_whitespace_only_bearer_token_returns_missing_token() -> None:
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/api/v1/test", headers={"Authorization": "Bearer    "})
    assert response.status_code == 401
    assert response.json()["type"] == "/errors/auth/missing-token"


# ─── P2 — CORS preflight (OPTIONS) bypasses auth middleware ───────────────────


def test_options_preflight_bypasses_auth() -> None:
    """Browser CORS preflight must pass auth middleware without credentials."""
    client = TestClient(_make_app(), raise_server_exceptions=False)
    # No Authorization header — typical preflight
    response = client.options("/api/v1/test")
    # Inner CORS middleware would normally handle this, but our minimal test
    # app has no CORSMiddleware. The point is auth must NOT 401 — it should
    # pass through to call_next which returns 405 (Method Not Allowed) since
    # no OPTIONS handler is registered. The key assertion is "not 401".
    assert response.status_code != 401


# ─── P1 — Defensive: missing app.state.auth_token_hash returns 503 not 500 ───


def test_missing_auth_state_returns_503() -> None:
    """Mini-app that did not run lifespan → 503 not 500."""
    app = FastAPI()
    # Intentionally do NOT set app.state.auth_token_hash.
    app.add_middleware(AuthTokenMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/api/v1/test")
    async def _endpoint() -> dict[str, str]:
        return {"ok": "true"}

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/v1/test", headers={"Authorization": "Bearer x"})
    assert response.status_code == 503
    assert response.json()["type"] == "/errors/auth/not-configured"
