"""End-to-end integration tests — `GET /workflows/runs/{run_id}/instances` (Story 2.4 T7.2).

Couvre AC3 :
* GET happy 200 avec 2 instances ordonnées created_at ASC.
* GET 200 + [] si run sans instances.
* GET 404 si run inexistant (sémantique strict — pas une liste vide).
* GET 422 si run_id non-UUID.
* Une instance hors run (workflow_run_id=None) n'apparaît PAS dans le list.

Fixtures workflow + workflow_run sont créées via `WorkflowRepo` +
`WorkflowRunRepo` direct (pas d'endpoint POST /workflows Sprint 1, cf
Story 2.4 §"Décisions intégrées" #13).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.repositories import WorkflowRepo, WorkflowRunRepo

# P-09 (CR 2026-05-10) — _make_app + _auth_headers factor dans conftest.py partagé.
from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app


async def _create_workflow_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    workflow_name: str,
) -> UUID:
    """Fixture helper — crée un Workflow + WorkflowRun via repos directs.

    Pas d'endpoint REST Sprint 1 (Story 4.1+). Le test simule ce que
    workflow_engine fera quand il créera des runs réels.
    """
    workflow_repo = WorkflowRepo(session_factory=factory)
    workflow = await workflow_repo.create(
        name=workflow_name,
        dag={"steps": []},  # placeholder Sprint 1.
    )
    workflow_run_repo = WorkflowRunRepo(session_factory=factory)
    run = await workflow_run_repo.create(
        workflow_id=workflow.id,
        correlation_id=uuid4(),
    )
    return run.id


async def _create_template(
    client: httpx.AsyncClient, *, name: str, archetype: str = "producteur"
) -> str:
    resp = await client.post(
        "/api/v1/agents/templates",
        headers=_auth_headers(),
        json={"archetype": archetype, "name": name},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["template_id"]


async def _instantiate(
    client: httpx.AsyncClient,
    template_id: str,
    *,
    workflow_run_id: UUID | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if workflow_run_id is not None:
        body["workflow_run_id"] = str(workflow_run_id)
    resp = await client.post(
        f"/api/v1/agents/templates/{template_id}/instances",
        headers=_auth_headers(),
        json=body,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.integration
async def test_list_instances_by_run_returns_2_ordered_by_created_at(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 — 2 instances rattachées au run, ordonnées created_at ASC."""
    run_id = await _create_workflow_run(seed_session_factory, workflow_name="ac3-list")
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _create_template(client, name="ac3-list-template")
        i1 = await _instantiate(client, template_id, workflow_run_id=run_id)
        i2 = await _instantiate(client, template_id, workflow_run_id=run_id)

        resp = await client.get(
            f"/api/v1/workflows/runs/{run_id}/instances",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 2
        assert body[0]["instance_id"] == i1["instance_id"]
        assert body[1]["instance_id"] == i2["instance_id"]


@pytest.mark.integration
async def test_list_instances_by_run_returns_empty_when_no_instances(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 — run existe mais 0 instances → 200 + []."""
    run_id = await _create_workflow_run(seed_session_factory, workflow_name="ac3-empty")
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/v1/workflows/runs/{run_id}/instances",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.json() == []


@pytest.mark.integration
async def test_list_instances_by_run_returns_404_when_run_missing(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 — run inexistant → 404 RFC 7807 (PAS une liste vide)."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/v1/workflows/runs/{uuid4()}/instances",
            headers=_auth_headers(),
        )
        assert resp.status_code == 404
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["status"] == 404
        assert "workflow run" in body.get("detail", "").lower()


@pytest.mark.integration
async def test_list_instances_by_run_excludes_orphan_instances(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 — instance avec workflow_run_id=None n'apparaît PAS dans le list."""
    run_id = await _create_workflow_run(seed_session_factory, workflow_name="ac3-orphan")
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _create_template(client, name="ac3-orphan-template")
        # 1 instance attachée au run, 1 instance orpheline.
        i_attached = await _instantiate(client, template_id, workflow_run_id=run_id)
        await _instantiate(client, template_id, workflow_run_id=None)

        resp = await client.get(
            f"/api/v1/workflows/runs/{run_id}/instances",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["instance_id"] == i_attached["instance_id"]


@pytest.mark.integration
async def test_list_instances_by_run_invalid_uuid_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """run_id non-UUID → 422 (FastAPI Path validation)."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/workflows/runs/not-a-uuid/instances",
            headers=_auth_headers(),
        )
        assert resp.status_code == 422
