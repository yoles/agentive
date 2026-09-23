"""Unit tests — `POST /api/v1/agents/templates` Pydantic validation (Story 2.1 T3.4).

Covers AC3 422 paths *without* hitting the DB. The repo + service are
mocked so we test only the request/response shape and the RFC 7807
content-type. End-to-end happy path lives in
``tests/integration/agent_registry/test_create_template_e2e.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentive_backend.app.main import create_app
from agentive_backend.features.agent_registry.schemas import CreateTemplateResponse
from agentive_backend.features.agent_registry.service import AgentTemplateService


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    # Story 2.1 P-14 — `monkeypatch.setenv` reverts env at teardown (vs.
    # `os.environ.setdefault` which leaks across the test session).
    monkeypatch.setenv("AGENTIVE_API_TOKEN", "test-static-token")
    monkeypatch.setenv("AGENTIVE_ENVIRONMENT", "development")
    yield create_app()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    from agentive_backend.features.agent_registry import load_registry

    app.state.archetype_registry = load_registry()
    # Story 2.1 P-08 — sentinel so `_build_service` doesn't 503. The unknown-
    # archetype path raises ValidationError BEFORE the repo is touched, and
    # the happy-path test patches `_build_service` so the sentinel is never
    # dereferenced.
    app.state.session_factory = MagicMock(name="unused-session-factory-stub")
    app.state.auth_token_hash = "test-static-token"
    return TestClient(app, raise_server_exceptions=False)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-static-token"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_post_template_missing_archetype_returns_422_rfc7807(client: TestClient) -> None:
    """AC3 — Missing `archetype` field → 422 RFC 7807."""
    resp = client.post(
        "/api/v1/agents/templates",
        headers=_auth_headers(),
        json={"name": "Test Template"},
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert body["type"] == "/errors/validation"
    assert body["status"] == 422
    assert "errors" in body  # Pydantic v2 detailed errors


def test_post_template_missing_name_returns_422_rfc7807(client: TestClient) -> None:
    """AC3 — Missing `name` field → 422 RFC 7807."""
    resp = client.post(
        "/api/v1/agents/templates",
        headers=_auth_headers(),
        json={"archetype": "producteur"},
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"


def test_post_template_empty_name_returns_422_rfc7807(client: TestClient) -> None:
    """AC3 — Empty name (min_length=1) → 422 RFC 7807."""
    resp = client.post(
        "/api/v1/agents/templates",
        headers=_auth_headers(),
        json={"archetype": "producteur", "name": ""},
    )
    assert resp.status_code == 422


def test_post_template_too_long_name_returns_422_rfc7807(client: TestClient) -> None:
    """AC3 — Name > 255 chars → 422 RFC 7807."""
    resp = client.post(
        "/api/v1/agents/templates",
        headers=_auth_headers(),
        json={"archetype": "producteur", "name": "x" * 256},
    )
    assert resp.status_code == 422


def test_post_template_extra_fields_rejected(client: TestClient) -> None:
    """AC3 + §"Pièges connus" #8 — extra fields (e.g. `config`) → 422."""
    resp = client.post(
        "/api/v1/agents/templates",
        headers=_auth_headers(),
        json={
            "archetype": "producteur",
            "name": "Test",
            "config": {"hijacked": "yes"},
        },
    )
    assert resp.status_code == 422


def test_post_template_unknown_archetype_returns_422_with_valid_list(
    client: TestClient, app: FastAPI
) -> None:
    """AC3 — Unknown archetype → ValidationError 422 RFC 7807 with valid list."""
    # Mock the service to assert the request reaches the validation path.
    # We use the real registry, so the service raises ValidationError.
    resp = client.post(
        "/api/v1/agents/templates",
        headers=_auth_headers(),
        json={"archetype": "not_an_archetype", "name": "Test"},
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert body["type"] == "/errors/validation"
    detail = body.get("detail", "")
    # Detail must enumerate the 8 valid archetypes for operator clarity.
    assert "producteur" in detail
    assert "controleur" in detail
    assert "Unknown archetype" in detail


def test_post_template_happy_path_uses_service(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """AC3 — Happy path 201 with mocked service."""
    template_id = uuid4()
    fake_response = CreateTemplateResponse(
        template_id=template_id,
        name="Code Producer",
        archetype="producteur",
        version=1,
        created_at=datetime.now(tz=UTC),
    )
    fake_service_instance = AsyncMock(spec=AgentTemplateService)
    fake_service_instance.create_template.return_value = fake_response

    # Patch the build helper so any caller (router) gets our mock.
    # NOTE: the package re-exports the APIRouter as `router` from
    # ``__init__.py``, so the dotted path
    # ``agentive_backend.features.agent_registry.router`` is ambiguous.
    # We import the submodule via importlib to disambiguate.
    import importlib

    router_module = importlib.import_module("agentive_backend.features.agent_registry.router")
    monkeypatch.setattr(
        router_module, "_build_template_service", lambda _request: fake_service_instance
    )

    resp = client.post(
        "/api/v1/agents/templates",
        headers=_auth_headers(),
        json={"archetype": "producteur", "name": "Code Producer"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["template_id"] == str(template_id)
    assert body["name"] == "Code Producer"
    assert body["archetype"] == "producteur"
    assert body["version"] == 1
    assert "created_at" in body


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Story 2.2 — GET + PUT /agents/templates/{id}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _patch_service(monkeypatch: pytest.MonkeyPatch, mock_service: AsyncMock) -> None:
    import importlib

    router_module = importlib.import_module("agentive_backend.features.agent_registry.router")
    monkeypatch.setattr(router_module, "_build_template_service", lambda _request: mock_service)


def test_put_template_invalid_uuid_returns_422(client: TestClient) -> None:
    """AC1 / D4 — UUID invalide en path ⇒ 422 (FastAPI Path UUID auto-validate)."""
    resp = client.put(
        "/api/v1/agents/templates/not-a-uuid",
        headers=_auth_headers(),
        json={"system_prompt": "x"},
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"


def test_put_template_extra_field_rejected(client: TestClient) -> None:
    """AC1 — extra=forbid ⇒ 422 sur champ inconnu."""
    resp = client.put(
        f"/api/v1/agents/templates/{uuid4()}",
        headers=_auth_headers(),
        json={"system_prompt": "x", "rogue": "field"},
    )
    assert resp.status_code == 422


def test_put_template_invalid_llm_model_returns_422(client: TestClient) -> None:
    """AC1 — llm_model hors whitelist ⇒ 422."""
    resp = client.put(
        f"/api/v1/agents/templates/{uuid4()}",
        headers=_auth_headers(),
        json={"llm_model": "gpt-7-omega"},
    )
    assert resp.status_code == 422


def test_put_template_happy_path_uses_service(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """AC1 — Happy path 200 avec mock service."""
    from agentive_backend.features.agent_registry.schemas import UpdateTemplateResponse

    template_id = uuid4()
    fake_response = UpdateTemplateResponse(
        template_id=template_id,
        name="Code Producer",
        archetype="producteur",
        version=2,
        config={"system_prompt": "v2 prompt", "llm_model": "claude-3-5-sonnet-20241022"},
        updated_at=datetime.now(tz=UTC),
    )
    fake_service = AsyncMock(spec=AgentTemplateService)
    fake_service.update_template.return_value = fake_response
    _patch_service(monkeypatch, fake_service)

    resp = client.put(
        f"/api/v1/agents/templates/{template_id}",
        headers=_auth_headers(),
        json={
            "system_prompt": "v2 prompt",
            "llm_model": "claude-3-5-sonnet-20241022",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 2
    assert body["template_id"] == str(template_id)
    assert "updated_at" in body


def test_get_template_invalid_uuid_returns_422(client: TestClient) -> None:
    """AC6 — UUID invalide en path ⇒ 422."""
    resp = client.get(
        "/api/v1/agents/templates/abc",
        headers=_auth_headers(),
    )
    assert resp.status_code == 422


def test_get_template_happy_path_uses_service(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """AC6 — Happy path 200 avec mock service."""
    from agentive_backend.features.agent_registry.schemas import TemplateDetailResponse

    template_id = uuid4()
    fake_response = TemplateDetailResponse(
        template_id=template_id,
        name="Code Producer",
        archetype="producteur",
        version=1,
        config={"prompt_base": "x", "role": "producer"},
        created_at=datetime.now(tz=UTC),
    )
    fake_service = AsyncMock(spec=AgentTemplateService)
    fake_service.get_template_by_id.return_value = fake_response
    _patch_service(monkeypatch, fake_service)

    resp = client.get(
        f"/api/v1/agents/templates/{template_id}",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["template_id"] == str(template_id)
    assert body["version"] == 1
