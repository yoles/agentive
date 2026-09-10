"""End-to-end integration tests — ``POST /api/v1/workflows`` (Story 4.1 T8.6).

Agent templates are seeded directly via ``AgentTemplateRepo`` (not through
the HTTP API) — mirrors ``tests/integration/memory_manager/test_api.py``'s
approach for namespaces, since only the archetype + config shape matter
here, not the archetype-registry skeleton.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.repositories import AgentTemplateRepo

from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app

pytestmark = pytest.mark.integration


async def _create_template(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    archetype: str = "producteur",
    output_contract: dict[str, Any] | None = None,
    llm_model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> Any:
    config: dict[str, Any] = {}
    if output_contract is not None:
        config["output_contract"] = output_contract
    if llm_model is not None:
        config["llm_model"] = llm_model
        config["llm_params"] = {"temperature": temperature, "max_tokens": max_tokens}
    repo = AgentTemplateRepo(session_factory=session_factory)
    return await repo.create(name=f"tpl-{uuid4()}", archetype=archetype, config=config)


async def _count_outbox(
    factory: async_sessionmaker[AsyncSession], event_type: str, *, workflow_id: str
) -> int:
    sql = (
        "SELECT COUNT(*) FROM outbox_events WHERE event_type = :t AND payload->>'workflow_id' = :w"
    )
    async with factory() as session:
        result = await session.execute(text(sql), {"t": event_type, "w": workflow_id})
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_create_workflow_happy_path_returns_201_and_persists_dag(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 + AC2 — nominal path : 201, dag persisted, outbox event."""
    tpl_a = await _create_template(app_session_factory)
    tpl_b = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "ingest-pipeline",
                "nodes": [
                    {"node_id": "a", "agent_template_id": str(tpl_a.id)},
                    {"node_id": "b", "agent_template_id": str(tpl_b.id)},
                ],
                "edges": [{"from_node_id": "a", "to_node_id": "b"}],
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == 1
    assert body["warnings"] == []
    workflow_id = body["workflow_id"]

    async with seed_session_factory() as session:
        result = await session.execute(
            text("SELECT dag, version, status FROM workflows WHERE id = :id"),
            {"id": workflow_id},
        )
        row = result.one()
        dag, version, status_ = row
        assert version == 1
        assert status_ == "active"
        assert {n["node_id"] for n in dag["nodes"]} == {"a", "b"}
        assert dag["edges"] == [{"from_node_id": "a", "to_node_id": "b", "condition": None}]

    count = await _count_outbox(
        seed_session_factory, "workflow_engine.workflow.created", workflow_id=workflow_id
    )
    assert count == 1


@pytest.mark.asyncio
async def test_create_workflow_cycle_returns_422_rfc7807(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 — cycle refusé en 422, aucune row persistée."""
    tpl_a = await _create_template(app_session_factory)
    tpl_b = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "cyclic-flow",
                "nodes": [
                    {"node_id": "a", "agent_template_id": str(tpl_a.id)},
                    {"node_id": "b", "agent_template_id": str(tpl_b.id)},
                ],
                "edges": [
                    {"from_node_id": "a", "to_node_id": "b"},
                    {"from_node_id": "b", "to_node_id": "a"},
                ],
            },
        )
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert body["type"] == "/errors/validation"
    assert "cycle" in body["detail"].lower()

    async with seed_session_factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM workflows WHERE name = 'cyclic-flow'")
        )
        assert int(result.scalar_one()) == 0


@pytest.mark.asyncio
async def test_create_workflow_unknown_template_returns_422_rfc7807(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 — agent_template_id inconnu ⇒ 422 (pas 404, cf Dev Notes)."""
    unknown_id = uuid4()
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "missing-template",
                "nodes": [{"node_id": "a", "agent_template_id": str(unknown_id)}],
                "edges": [],
            },
        )
    assert resp.status_code == 422
    body = resp.json()
    assert body["type"] == "/errors/validation"
    assert str(unknown_id) in body["detail"]

    async with seed_session_factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM workflows WHERE name = 'missing-template'")
        )
        assert int(result.scalar_one()) == 0


@pytest.mark.asyncio
async def test_create_workflow_condition_variable_not_exposed_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 — variable de branchement non exposée par output_contract.core ⇒ 422."""
    tpl_a = await _create_template(app_session_factory, output_contract={"core": {}, "extras": {}})
    tpl_b = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "bad-condition",
                "nodes": [
                    {"node_id": "a", "agent_template_id": str(tpl_a.id)},
                    {"node_id": "b", "agent_template_id": str(tpl_b.id)},
                ],
                "edges": [
                    {"from_node_id": "a", "to_node_id": "b", "condition": "output.status == 'ok'"}
                ],
            },
        )
    assert resp.status_code == 422
    body = resp.json()
    assert body["type"] == "/errors/validation"
    assert "output.status" in body["detail"]

    async with seed_session_factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM workflows WHERE name = 'bad-condition'")
        )
        assert int(result.scalar_one()) == 0


@pytest.mark.asyncio
async def test_create_workflow_diversity_warning_is_non_blocking(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC4 — même config LLM Contrôleur/Producteur ⇒ 201 + warnings non vide (D84)."""
    producer = await _create_template(
        app_session_factory, archetype="producteur", llm_model="claude-3-5-sonnet-20241022"
    )
    controller = await _create_template(
        app_session_factory, archetype="controleur", llm_model="claude-3-5-sonnet-20241022"
    )
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": "diversity-flow",
                "nodes": [
                    {"node_id": "producer", "agent_template_id": str(producer.id)},
                    {"node_id": "controller", "agent_template_id": str(controller.id)},
                ],
                "edges": [{"from_node_id": "producer", "to_node_id": "controller"}],
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["warnings"]) == 1
    warning = body["warnings"][0]
    assert warning["code"] == "llm_diversity"
    assert warning["controller_node_id"] == "controller"
    assert warning["producer_node_id"] == "producer"

    # Non-blocking — the workflow row IS persisted despite the warning.
    async with seed_session_factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM workflows WHERE name = 'diversity-flow'")
        )
        assert int(result.scalar_one()) == 1
