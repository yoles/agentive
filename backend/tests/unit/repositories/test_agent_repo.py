"""Unit tests — :class:`AgentTemplateRepo` and :class:`AgentInstanceRepo`."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentive_backend.shared.repositories import AgentInstanceRepo, AgentTemplateRepo

from .conftest import make_session_factory_mock


@pytest.mark.asyncio
async def test_agent_template_create_defaults_version_to_one() -> None:
    factory, session = make_session_factory_mock()
    repo = AgentTemplateRepo(session_factory=factory)
    await repo.create(name="dev-lead", archetype="orchestrator", config={})
    template = session.add.call_args.args[0]
    assert template.version == 1


@pytest.mark.asyncio
async def test_agent_template_get_by_name_version_executes_select() -> None:
    factory, session = make_session_factory_mock()
    repo = AgentTemplateRepo(session_factory=factory)
    await repo.get_by_name_version("dev-lead", 1)
    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "agent_templates" in sql_text


@pytest.mark.asyncio
async def test_agent_instance_create_records_template_version_snapshot() -> None:
    factory, session = make_session_factory_mock()
    repo = AgentInstanceRepo(session_factory=factory)
    template_id = uuid4()
    await repo.create(template_id=template_id, template_version=3, snapshot={"k": "v"})
    instance = session.add.call_args.args[0]
    assert instance.template_id == template_id
    assert instance.template_version == 3
    assert instance.snapshot == {"k": "v"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Story 2.2 — update_in_session, update_config_in_session, get_by_id_in_session
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_update_in_session_bumps_version_and_config() -> None:
    """Story 2.2 T2.1 — update mute la row + flush sans rouvrir de transaction."""
    _, session = make_session_factory_mock()
    repo = AgentTemplateRepo(session_factory=lambda: None)  # factory non utilisée ici
    template = SimpleNamespace(name="prod", config={"prompt_base": "old"}, version=1)
    new_config = {"prompt_base": "old", "system_prompt": "new"}
    result = await repo.update_in_session(
        session, template=template, config=new_config, new_version=2
    )
    assert result.config == new_config
    assert result.version == 2
    session.flush.assert_awaited_once()
    session.refresh.assert_awaited_once_with(template)


@pytest.mark.asyncio
async def test_update_config_in_session_no_version_bump() -> None:
    """Story 2.2 T2.1 — update_config (pas de bump version)."""
    _, session = make_session_factory_mock()
    repo = AgentTemplateRepo(session_factory=lambda: None)
    template = SimpleNamespace(name="prod", config={"prompt_base": "x"}, version=5)
    new_config = {"prompt_base": "x", "llm_params": {"temperature": 0.1}}
    result = await repo.update_config_in_session(session, template=template, config=new_config)
    assert result.config == new_config
    assert result.version == 5  # inchangée
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_by_id_in_session_uses_session_get() -> None:
    """Story 2.2 T2.1 — get_by_id_in_session délègue à session.get."""
    _, session = make_session_factory_mock()
    repo = AgentTemplateRepo(session_factory=lambda: None)
    template_id = uuid4()
    await repo.get_by_id_in_session(session, template_id)
    session.get.assert_awaited_once()
    args = session.get.await_args.args
    assert args[1] == template_id
