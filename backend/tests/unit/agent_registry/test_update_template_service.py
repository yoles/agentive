"""Unit tests — :meth:`AgentRegistryService.update_template` (Story 2.2 T4.2).

Le test e2e d'atomicité (rollback complet sur publish failure) vit dans
``tests/integration/agent_registry/test_update_template_e2e.py``. Ici on
couvre la mécanique de merge / bump version avec un mock de session sans
toucher Postgres.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agentive_backend.features.agent_registry.schemas import (
    ContractDefinition,
    ErrorPolicy,
    LLMParams,
    UpdateTemplateRequest,
)
from agentive_backend.features.agent_registry.service import AgentRegistryService
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
) -> tuple[AgentRegistryService, AsyncMock, AsyncMock, AsyncMock]:
    """Build a service with mocked repos. Returns (service, template_repo, prompt_repo, session)."""
    session_mock = AsyncMock()
    session_mock.flush = AsyncMock()
    session_mock.refresh = AsyncMock()
    session_mock.add = MagicMock()

    @asynccontextmanager
    async def _with_tenant(_tenant_id: Any) -> AsyncIterator[AsyncMock]:
        yield session_mock

    template_repo = AsyncMock()
    template_repo.with_tenant = (
        _with_tenant  # MagicMock would call it once per await — keep async-cm
    )
    template_repo.get_by_id_in_session = AsyncMock(return_value=template)

    # Mirror the real repo: mutate the template attributes then return it,
    # so the service's downstream code (`updated.version`, `updated.config`)
    # observes the new values.
    async def _mock_update(
        _session: object, *, template: Any, config: dict, new_version: int
    ) -> Any:  # type: ignore[no-untyped-def]
        template.config = config
        template.version = new_version
        return template

    async def _mock_update_config(_session: object, *, template: Any, config: dict) -> Any:  # type: ignore[no-untyped-def]
        template.config = config
        return template

    template_repo.update_in_session = AsyncMock(side_effect=_mock_update)
    template_repo.update_config_in_session = AsyncMock(side_effect=_mock_update_config)

    prompt_repo = AsyncMock()
    prompt_repo.create_in_session = AsyncMock()

    # Story 2.4 + 2.5 — service constructor expanded ; these aren't exercised
    # by update_template, so plain AsyncMocks are sufficient.
    instance_repo = AsyncMock()
    workflow_run_repo = AsyncMock()
    tool_repo = AsyncMock()
    assignment_repo = AsyncMock()

    service = AgentRegistryService(
        registry={},  # not used in update_template paths
        template_repo=template_repo,
        prompt_repo=prompt_repo,
        instance_repo=instance_repo,
        workflow_run_repo=workflow_run_repo,
        tool_repo=tool_repo,
        assignment_repo=assignment_repo,
    )
    return service, template_repo, prompt_repo, session_mock


@pytest.mark.asyncio
async def test_update_template_not_found_raises(event_publish_mock: AsyncMock) -> None:
    """AC1 — template inexistant ⇒ NotFoundError (404 RFC 7807 côté route)."""
    service, _trepo, prepo, _session = _make_service(template=None)
    with pytest.raises(NotFoundError, match="not found"):
        await service.update_template(uuid4(), UpdateTemplateRequest(system_prompt="x"))
    prepo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_template_with_system_prompt_bumps_version_and_inserts_prompt(
    event_publish_mock: AsyncMock,
) -> None:
    """AC1 — system_prompt présent ⇒ version bump + prompt insert + event published."""
    template = SimpleNamespace(
        id=uuid4(),
        name="Code Producer",
        archetype="producteur",
        version=1,
        config={"prompt_base": "old base", "role": "producer"},
    )
    service, trepo, prepo, _session = _make_service(template=template)

    payload = UpdateTemplateRequest(
        system_prompt="v2 prompt",
        llm_params=LLMParams(temperature=0.3, max_tokens=2048),
        error_policy=ErrorPolicy(max_retries=5),
    )
    response = await service.update_template(template.id, payload)

    # Version bump
    assert response.version == 2
    # Prompt insert called with version=2
    prepo.create_in_session.assert_awaited_once()
    assert prepo.create_in_session.await_args.kwargs["version"] == 2
    assert prepo.create_in_session.await_args.kwargs["content"] == "v2 prompt"
    # Template UPDATE called (not update_config_in_session)
    trepo.update_in_session.assert_awaited_once()
    trepo.update_config_in_session.assert_not_awaited()
    # Event published with old/new versions
    event_publish_mock.assert_awaited_once()
    event_arg = event_publish_mock.await_args.args[1]  # second positional = event payload
    assert event_arg.old_version == 1
    assert event_arg.new_version == 2
    # Config merged
    assert response.config["system_prompt"] == "v2 prompt"
    assert response.config["llm_params"] == {"temperature": 0.3, "max_tokens": 2048}
    assert response.config["error_policy"]["max_retries"] == 5
    # Champ absent du payload reste intact
    assert response.config["prompt_base"] == "old base"


@pytest.mark.asyncio
async def test_update_template_without_system_prompt_no_bump_no_prompt_insert(
    event_publish_mock: AsyncMock,
) -> None:
    """AC1 — payload sans system_prompt ⇒ pas de bump, pas d'insert prompt, event quand même."""
    template = SimpleNamespace(
        id=uuid4(),
        name="x",
        archetype="producteur",
        version=5,
        config={"prompt_base": "kept"},
    )
    service, trepo, prepo, _session = _make_service(template=template)

    payload = UpdateTemplateRequest(
        llm_model="claude-3-5-haiku-20241022",
        provider_chain=["anthropic", "openai"],
    )
    response = await service.update_template(template.id, payload)

    assert response.version == 5  # no bump
    prepo.create_in_session.assert_not_awaited()
    trepo.update_config_in_session.assert_awaited_once()
    trepo.update_in_session.assert_not_awaited()
    event_publish_mock.assert_awaited_once()
    event_arg = event_publish_mock.await_args.args[1]
    assert event_arg.old_version == 5
    assert event_arg.new_version == 5
    assert response.config["llm_model"] == "claude-3-5-haiku-20241022"
    assert response.config["provider_chain"] == ["anthropic", "openai"]


@pytest.mark.asyncio
async def test_update_template_merges_contracts_via_model_dump(
    event_publish_mock: AsyncMock,
) -> None:
    """AC2 — input/output_contract sérialisés via model_dump (core + extras)."""
    template = SimpleNamespace(id=uuid4(), name="x", archetype="producteur", version=1, config={})
    service, _trepo, _prepo, _session = _make_service(template=template)

    payload = UpdateTemplateRequest(
        input_contract=ContractDefinition(core={"q": "string"}, extras={"meta": "ok"}),
        output_contract=ContractDefinition(core={"a": "string"}, extras={}),
    )
    response = await service.update_template(template.id, payload)

    assert response.config["input_contract"] == {
        "core": {"q": "string"},
        "extras": {"meta": "ok"},
    }
    assert response.config["output_contract"] == {"core": {"a": "string"}, "extras": {}}


@pytest.mark.asyncio
async def test_update_template_skip_bump_when_system_prompt_unchanged_p01(
    event_publish_mock: AsyncMock,
) -> None:
    """P-01 fix Story 2.2 review — system_prompt identique à l'existant ⇒
    pas de bump version, pas d'insert prompts row. L'event reste publié
    pour tracer l'intention (config peut quand même avoir changé via d'autres
    champs comme llm_params)."""
    template = SimpleNamespace(
        id=uuid4(),
        name="x",
        archetype="producteur",
        version=3,
        config={"system_prompt": "same prompt", "prompt_base": "skel"},
    )
    service, trepo, prepo, _session = _make_service(template=template)

    payload = UpdateTemplateRequest(
        system_prompt="same prompt",  # IDENTIQUE à l'existant
        llm_params=LLMParams(temperature=0.9, max_tokens=1024),
    )
    response = await service.update_template(template.id, payload)

    assert response.version == 3  # no bump
    prepo.create_in_session.assert_not_awaited()  # no prompts row
    trepo.update_in_session.assert_not_awaited()
    trepo.update_config_in_session.assert_awaited_once()
    # l'event est tout de même publié (l'opérateur a changé llm_params)
    event_publish_mock.assert_awaited_once()
    event_arg = event_publish_mock.await_args.args[1]
    assert event_arg.old_version == 3
    assert event_arg.new_version == 3
