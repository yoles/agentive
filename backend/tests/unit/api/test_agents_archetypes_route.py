"""Unit tests — `GET /api/v1/agents/archetypes` + `/:id` (Story 2.1 T3.4)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentive_backend.app.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    """Build a FastAPI app without running the lifespan (no DB, no LLM keys).

    We bypass the lifespan by NOT starting the TestClient as a context
    manager. The archetype registry is then injected manually via
    ``app.state``. This isolates the route logic from DB dependencies for
    unit tests.

    Story 2.1 P-14 — `monkeypatch.setenv` reverts env at fixture teardown
    (vs. `os.environ.setdefault` which leaks across tests and pollutes
    other tests that rely on absent env vars, e.g. fail-fast token init).
    """
    monkeypatch.setenv("AGENTIVE_API_TOKEN", "test-static-token")  # plaintext OK in dev/test
    monkeypatch.setenv("AGENTIVE_ENVIRONMENT", "development")
    test_app = create_app()
    yield test_app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """TestClient WITHOUT lifespan startup (no DB/LLM init). For pure
    route logic tests we inject ``app.state`` manually.

    Story 2.1 P-08 — ``_build_service`` raises 503 if ``session_factory``
    is ``None``. The GET archetype endpoints don't actually use the repo,
    but the wiring still requires the attribute to be present. We use a
    sentinel `object()` that is truthy and never called for GET routes.
    """
    from unittest.mock import MagicMock

    from agentive_backend.features.agent_registry import load_registry

    app.state.archetype_registry = load_registry()
    app.state.session_factory = MagicMock(name="unused-session-factory-stub")
    # Bypass lifespan auth init — plaintext mode acceptable in dev/test.
    app.state.auth_token_hash = "test-static-token"
    return TestClient(app, raise_server_exceptions=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_list_archetypes_returns_8_summaries(client: TestClient) -> None:
    """AC2 — GET /agents/archetypes returns 8 lean entries (no prompt_base)."""
    resp = client.get(
        "/api/v1/agents/archetypes",
        headers={"Authorization": "Bearer test-static-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 8
    expected_ids = {
        "orchestrateur",
        "chercheur",
        "analyste",
        "producteur",
        "stratege",
        "controleur",
        "veilleur",
        "communicateur",
    }
    assert {a["id"] for a in body} == expected_ids
    # Ensure prompt_base is NOT in the lean summary
    for archetype in body:
        assert "prompt_base" not in archetype
        assert "input_contract" not in archetype


def test_get_archetype_detail_returns_prompt_base_and_contracts(client: TestClient) -> None:
    """AC2 — GET /agents/archetypes/{id} returns full archetype detail."""
    resp = client.get(
        "/api/v1/agents/archetypes/producteur",
        headers={"Authorization": "Bearer test-static-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "producteur"
    assert body["display_name"] == "Producteur"
    assert "prompt_base" in body and len(body["prompt_base"]) > 0
    assert "input_contract" in body
    assert "output_contract" in body
    assert "core" in body["input_contract"]
    assert "extras" in body["input_contract"]


def test_get_unknown_archetype_returns_404_rfc7807(client: TestClient) -> None:
    """AC2 — Unknown archetype id → 404 RFC 7807."""
    resp = client.get(
        "/api/v1/agents/archetypes/unknown_archetype",
        headers={"Authorization": "Bearer test-static-token"},
    )
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert body["type"] == "/errors/not-found"
    assert body["title"] == "Resource not found"
    assert body["status"] == 404
    assert "unknown_archetype" in body.get("detail", "")


def test_list_archetypes_requires_auth(client: TestClient) -> None:
    """AC2 — Missing Bearer → 401."""
    resp = client.get("/api/v1/agents/archetypes")
    assert resp.status_code == 401
