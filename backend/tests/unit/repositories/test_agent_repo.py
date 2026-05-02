"""Unit tests — :class:`AgentTemplateRepo` and :class:`AgentInstanceRepo`."""

from __future__ import annotations

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
