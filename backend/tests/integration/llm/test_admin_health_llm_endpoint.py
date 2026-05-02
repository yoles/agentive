"""``/api/v1/admin/health/llm`` — loopback gating + provider introspection."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentive_backend.api.admin import llm_health_router
from agentive_backend.shared.llm import LLMRouter
from agentive_backend.shared.llm.testing import MockProvider
from agentive_backend.shared.llm.types import Completion


def _completion() -> Completion:
    return Completion(
        text="ok",
        model="mock-model",
        provider="mock",
        input_tokens=0,
        output_tokens=1,
        finish_reason="stop",
        latency_ms=1.0,
        cost_estimate_usd=Decimal("0"),
    )


def _make_app() -> FastAPI:
    app = FastAPI()
    mock = MockProvider("mock", [_completion() for _ in range(64)])
    app.state.llm_router = LLMRouter(providers={"mock": mock}, default_chain=["mock"])
    app.include_router(llm_health_router, prefix="/api/v1/admin")
    return app


def test_returns_200_from_localhost() -> None:
    app = _make_app()
    with TestClient(app) as client:
        response = client.get("/api/v1/admin/health/llm")
        assert response.status_code == 200
        body = response.json()
        assert body["default_chain"] == ["mock"]
        names = {p["name"] for p in body["providers"]}
        assert names == {"mock"}


@pytest.mark.asyncio
async def test_rejects_non_loopback_with_403() -> None:
    """Simulate a remote IP via ``Request.client.host`` overrides."""
    from agentive_backend.api.admin import health_llm

    app = _make_app()

    # Patch the loopback predicate to simulate non-local origin.
    original = health_llm._is_local_request
    try:
        health_llm._is_local_request = lambda _r: False  # type: ignore[assignment]
        with TestClient(app) as client:
            response = client.get("/api/v1/admin/health/llm")
            assert response.status_code == 403
    finally:
        health_llm._is_local_request = original  # type: ignore[assignment]
