"""Unit tests — :class:`PromptRepo`."""

from __future__ import annotations

from uuid import uuid4

import pytest

from agentive_backend.shared.repositories import PromptRepo

from .conftest import make_session_factory_mock


@pytest.mark.asyncio
async def test_create_persists_versioned_prompt() -> None:
    factory, session = make_session_factory_mock()
    repo = PromptRepo(session_factory=factory)
    template_id = uuid4()
    await repo.create(agent_template_id=template_id, version=2, content="say hi")
    prompt = session.add.call_args.args[0]
    assert prompt.agent_template_id == template_id
    assert prompt.version == 2
    assert prompt.content == "say hi"


@pytest.mark.asyncio
async def test_get_by_template_version_executes_select() -> None:
    factory, session = make_session_factory_mock()
    repo = PromptRepo(session_factory=factory)
    await repo.get_by_template_version(uuid4(), 2)
    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "prompts" in sql_text


@pytest.mark.asyncio
async def test_create_in_session_persists_versioned_prompt() -> None:
    """Story 2.2 T2.2 — create_in_session ajoute la row sans rouvrir transaction."""
    _, session = make_session_factory_mock()
    repo = PromptRepo(session_factory=lambda: None)
    template_id = uuid4()
    await repo.create_in_session(
        session, agent_template_id=template_id, version=3, content="v3 prompt"
    )
    prompt = session.add.call_args.args[0]
    assert prompt.agent_template_id == template_id
    assert prompt.version == 3
    assert prompt.content == "v3 prompt"
    session.flush.assert_awaited_once()
    session.refresh.assert_awaited_once_with(prompt)
