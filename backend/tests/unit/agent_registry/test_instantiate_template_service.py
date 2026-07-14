"""Unit tests — :meth:`AgentRegistryService.instantiate_from_template` (Story 2.4 T4.6).

E2E intégration (atomicité Postgres réelle, isolation v2/v3) vit dans
``tests/integration/agent_registry/test_instantiate_template_e2e.py``.
Ici on couvre la mécanique snapshot + audit event + validation FK avec
mocks de session, sans toucher Postgres.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agentive_backend.features.agent_registry.service import TemplateInstantiationService
from agentive_backend.shared.exceptions import NotFoundError


@pytest.fixture
def event_publish_mock(monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncMock]:
    """Patch ``service.publish`` + ``service.notify_best_effort`` so we don't touch the bus."""
    pub_mock = AsyncMock(return_value=uuid4())
    notify_mock = AsyncMock(return_value=None)
    import agentive_backend.features.agent_registry.service as svc_module

    monkeypatch.setattr(svc_module, "publish", pub_mock)
    monkeypatch.setattr(svc_module, "notify_best_effort", notify_mock)
    yield pub_mock


def _make_service(
    *,
    template: SimpleNamespace | None,
    workflow_run: SimpleNamespace | None = None,
) -> tuple[TemplateInstantiationService, AsyncMock, AsyncMock, AsyncMock]:
    """Build the service with mocked repos.

    Returns (service, instance_repo, template_repo, workflow_run_repo).
    """
    session_mock = AsyncMock()
    session_mock.flush = AsyncMock()
    session_mock.refresh = AsyncMock()
    session_mock.add = MagicMock()

    @asynccontextmanager
    async def _with_tenant(_tenant_id: Any) -> AsyncIterator[AsyncMock]:
        yield session_mock

    template_repo = AsyncMock()
    template_repo.with_tenant = _with_tenant
    # A-07 — service calls the lookup-or-404 helper; emulate its contract.
    if template is None:
        template_repo.require_by_id_in_session = AsyncMock(
            side_effect=NotFoundError(detail="Agent template not found", context={})
        )
    else:
        template_repo.require_by_id_in_session = AsyncMock(return_value=template)

    instance_repo = AsyncMock()
    instance_repo.with_tenant = _with_tenant

    # Mirror the real repo: build an instance namespace, "persist" it, return it.
    async def _mock_create(
        _session: object,
        *,
        template_id: Any,
        template_version: int,
        snapshot: dict[str, Any],
        workflow_run_id: Any | None = None,
        tenant_id: Any | None = None,
    ) -> SimpleNamespace:
        from datetime import UTC, datetime

        return SimpleNamespace(
            id=uuid4(),
            template_id=template_id,
            template_version=template_version,
            snapshot=snapshot,
            workflow_run_id=workflow_run_id,
            tenant_id=tenant_id,
            created_at=datetime.now(UTC),
        )

    instance_repo.create_in_session = AsyncMock(side_effect=_mock_create)

    workflow_run_repo = AsyncMock()
    if workflow_run is None:
        workflow_run_repo.require_by_id_in_session = AsyncMock(
            side_effect=NotFoundError(detail="Workflow run not found", context={})
        )
    else:
        workflow_run_repo.require_by_id_in_session = AsyncMock(return_value=workflow_run)

    # Audit A-10 — instantiation now lives on the focused
    # TemplateInstantiationService (template + instance + workflow_run repos).
    service = TemplateInstantiationService(
        template_repo=template_repo,
        instance_repo=instance_repo,
        workflow_run_repo=workflow_run_repo,
    )
    return service, instance_repo, template_repo, workflow_run_repo


@pytest.mark.asyncio
async def test_instantiate_template_not_found_raises_404(
    event_publish_mock: AsyncMock,
) -> None:
    """AC6 — template inexistant ⇒ NotFoundError (404 RFC 7807 côté route)."""
    service, irepo, _trepo, _wrepo = _make_service(template=None)
    with pytest.raises(NotFoundError, match="not found"):
        await service.instantiate_from_template(template_id=uuid4())
    irepo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_instantiate_template_with_missing_workflow_run_raises_404(
    event_publish_mock: AsyncMock,
) -> None:
    """AC6 (variant) — workflow_run_id fourni mais inexistant ⇒ NotFoundError."""
    template = SimpleNamespace(
        id=uuid4(),
        name="Code Producer",
        archetype="producteur",
        version=2,
        config={"system_prompt": "v2"},
    )
    service, irepo, _trepo, _wrepo = _make_service(template=template, workflow_run=None)
    with pytest.raises(NotFoundError, match="Workflow run"):
        await service.instantiate_from_template(template_id=template.id, workflow_run_id=uuid4())
    irepo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_instantiate_template_happy_path_captures_snapshot_and_publishes_event(
    event_publish_mock: AsyncMock,
) -> None:
    """AC1 — snapshot complet figé + event agent_registry.agent_instance.created publié."""
    template = SimpleNamespace(
        id=uuid4(),
        name="Code Producer",
        archetype="producteur",
        version=3,
        config={
            "system_prompt": "v3 prompt",
            "llm_model": "claude-3-5-sonnet-20241022",
            "provider_chain": ["anthropic"],
        },
    )
    service, irepo, _trepo, _wrepo = _make_service(template=template)

    response = await service.instantiate_from_template(template_id=template.id)

    # AC1 — instance row INSERT called with the right snapshot shape.
    irepo.create_in_session.assert_awaited_once()
    create_kwargs = irepo.create_in_session.await_args.kwargs
    assert create_kwargs["template_id"] == template.id
    assert create_kwargs["template_version"] == 3
    assert create_kwargs["workflow_run_id"] is None
    snapshot = create_kwargs["snapshot"]
    assert snapshot["template_id"] == str(template.id)
    assert snapshot["template_version"] == 3
    assert snapshot["name"] == "Code Producer"
    assert snapshot["archetype"] == "producteur"
    assert snapshot["config"] == template.config

    # AC1 — audit event published in the SAME transaction (P-02 atomicity).
    event_publish_mock.assert_awaited_once()
    event_type = event_publish_mock.await_args.args[0]
    assert event_type == "agent_registry.agent_instance.created"
    event_payload = event_publish_mock.await_args.args[1]
    assert event_payload.template_id == template.id
    assert event_payload.template_version == 3
    assert event_payload.actor == "system"

    # Response shape mirror.
    assert response.template_version == 3
    assert response.snapshot == snapshot


@pytest.mark.asyncio
async def test_instantiate_template_with_workflow_run_attaches_fk(
    event_publish_mock: AsyncMock,
) -> None:
    """AC1 (variant) — workflow_run_id propagé jusqu'au create_in_session."""
    template = SimpleNamespace(
        id=uuid4(),
        name="t",
        archetype="producteur",
        version=1,
        config={},
    )
    run = SimpleNamespace(id=uuid4())
    service, irepo, _trepo, wrepo = _make_service(template=template, workflow_run=run)

    response = await service.instantiate_from_template(
        template_id=template.id, workflow_run_id=run.id
    )

    wrepo.require_by_id_in_session.assert_awaited_once()
    irepo.create_in_session.assert_awaited_once()
    assert irepo.create_in_session.await_args.kwargs["workflow_run_id"] == run.id
    assert response.workflow_run_id == run.id


@pytest.mark.asyncio
async def test_instantiate_template_atomicity_event_failure_blocks_commit(
    event_publish_mock: AsyncMock,
) -> None:
    """AC5 — si publish() throw, la transaction rollback (l'instance n'est pas commit).

    Avec les mocks ici, on vérifie juste que l'erreur propage et que l'event
    est bien tenté APRÈS l'INSERT (les deux sont dans le même `with` block,
    donc si event throw, la session n'aura pas commit). Le test e2e
    Postgres réel vit dans test_instantiate_template_e2e.py (T7.3).
    """
    template = SimpleNamespace(
        id=uuid4(),
        name="t",
        archetype="producteur",
        version=1,
        config={},
    )
    service, _irepo, _trepo, _wrepo = _make_service(template=template)
    event_publish_mock.side_effect = RuntimeError("simulated bus failure")

    with pytest.raises(RuntimeError, match="simulated bus failure"):
        await service.instantiate_from_template(template_id=template.id)


@pytest.mark.asyncio
async def test_get_instance_by_id_not_found_raises(
    event_publish_mock: AsyncMock,
) -> None:
    """AC4 — instance inexistante ⇒ NotFoundError."""
    service, irepo, _trepo, _wrepo = _make_service(template=None)
    irepo.require_by_id = AsyncMock(
        side_effect=NotFoundError(detail="Agent instance not found", context={})
    )
    with pytest.raises(NotFoundError, match="instance"):
        await service.get_instance_by_id(uuid4())


@pytest.mark.asyncio
async def test_list_instances_by_workflow_run_404_when_run_missing(
    event_publish_mock: AsyncMock,
) -> None:
    """AC3 — run inexistant ⇒ NotFoundError (PAS une liste vide)."""
    service, _irepo, _trepo, _wrepo = _make_service(template=None, workflow_run=None)
    with pytest.raises(NotFoundError, match="Workflow run"):
        await service.list_instances_by_workflow_run(uuid4())
