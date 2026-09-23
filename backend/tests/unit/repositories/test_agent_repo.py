"""Unit tests — :class:`AgentTemplateRepo` and :class:`AgentInstanceRepo`."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Story 2.4 — AgentInstanceRepo: create_in_session + list_by_workflow_run_in_session + get_by_id_in_session
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_agent_instance_create_in_session_adds_and_flushes() -> None:
    """Story 2.4 T1.1 — create_in_session ajoute la row dans la session du caller (no commit)."""
    _, session = make_session_factory_mock()
    repo = AgentInstanceRepo(session_factory=lambda: None)
    template_id = uuid4()
    snapshot = {"template_id": str(template_id), "template_version": 2, "config": {}}
    await repo.create_in_session(
        session,
        template_id=template_id,
        template_version=2,
        snapshot=snapshot,
    )
    session.add.assert_called_once()
    session.flush.assert_awaited_once()
    session.refresh.assert_awaited_once()
    # P-14 (CR 2026-05-10) — verrouille le contrat "caller owns commit"
    # (pas de commit dans create_in_session). Un futur sneaky commit
    # cassera ce test (et l'atomicité Story 2.1 P-02 partout).
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    instance = session.add.call_args.args[0]
    assert instance.template_id == template_id
    assert instance.template_version == 2
    assert instance.snapshot == snapshot


@pytest.mark.asyncio
async def test_agent_instance_get_by_id_in_session_uses_session_get() -> None:
    """Story 2.4 T1.3 — get_by_id_in_session délègue à session.get."""
    _, session = make_session_factory_mock()
    repo = AgentInstanceRepo(session_factory=lambda: None)
    instance_id = uuid4()
    await repo.get_by_id_in_session(session, instance_id)
    session.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_instance_list_by_workflow_run_executes_select_with_order() -> None:
    """Story 2.4 T1.2 — SELECT ordonné created_at ASC scoped au workflow_run."""
    _, session = make_session_factory_mock()
    repo = AgentInstanceRepo(session_factory=lambda: None)
    run_id = uuid4()
    await repo.list_by_workflow_run_in_session(session, run_id)
    session.execute.assert_awaited_once()
    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "agent_instances" in sql_text
    assert "workflow_run_id" in sql_text
    # P-13 (CR 2026-05-10) — verrouille la direction ASC explicite (sinon un
    # swap `desc()` futur ne casserait pas le test).
    assert "order by" in sql_text and "asc" in sql_text and "created_at" in sql_text
    # P-19 (CR 2026-05-10) — secondary sort sur id pour déterminisme tests
    # (cas Story 4.x where 2 INSERT batchés dans même tx auraient created_at identique).
    assert "agent_instances.id" in sql_text


# ─── Story 4.8 AC3 — batch resolution ──────────────────────────────────


def _scalars(session: object, rows: list[object]) -> None:
    """Point the mocked session's next `execute` at ``rows``."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session.execute.return_value = result  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_agent_template_list_by_ids_returns_a_map_keyed_by_id() -> None:
    """AC3 — every caller needs random access by id; rebuilding the map at
    each call site is how the two callers would drift."""
    factory, session = make_session_factory_mock()
    a, b = SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())
    _scalars(session, [a, b])
    repo = AgentTemplateRepo(session_factory=factory)

    resolved = await repo.list_by_ids([a.id, b.id])

    assert resolved == {a.id: a, b.id: b}
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_template_list_by_ids_deduplicates_repeated_ids() -> None:
    """AC3 — several DAG nodes may reference the same template; an `IN` list
    repeating an id is pure waste."""
    factory, session = make_session_factory_mock()
    shared = SimpleNamespace(id=uuid4())
    _scalars(session, [shared])
    repo = AgentTemplateRepo(session_factory=factory)

    await repo.list_by_ids([shared.id, shared.id, shared.id])

    rendered = str(
        session.execute.await_args.args[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert rendered.count(str(shared.id)) == 1


@pytest.mark.asyncio
async def test_agent_template_list_by_ids_skips_the_query_on_an_empty_input() -> None:
    """`IN ()` is a SQL syntax error in Postgres; SQLAlchemy renders a
    provably-false expression instead, so the query would be valid but
    pointless. Don't pay the round trip."""
    factory, session = make_session_factory_mock()
    repo = AgentTemplateRepo(session_factory=factory)

    assert await repo.list_by_ids([]) == {}

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_template_list_by_ids_in_session_does_not_lock_by_default() -> None:
    """A plain reader must never take a lock it did not ask for — the
    execution path (`_load_templates`) goes through this door."""
    _, session = make_session_factory_mock()
    _scalars(session, [])
    repo = AgentTemplateRepo(session_factory=lambda: None)

    await repo.list_by_ids_in_session(session, [uuid4()])

    rendered = str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "FOR SHARE" not in rendered


@pytest.mark.asyncio
async def test_agent_template_list_by_ids_in_session_emits_for_share_when_locked() -> None:
    """AC2 — this is the assertion that proves the TOCTOU window is closed.

    A timing-based test (create a workflow, race a `PUT` against it with
    sleeps) would pass by luck and fail in CI for unrelated reasons. What
    actually has to be true is that the resolution query carries `FOR SHARE`,
    so a concurrent `UPDATE`/`DELETE` on those rows blocks until the workflow
    INSERT commits. Assert that, on the compiled SQL — the same technique
    `test_workflow_repo.py` already uses for `claim_stale_running`.
    """
    _, session = make_session_factory_mock()
    _scalars(session, [])
    repo = AgentTemplateRepo(session_factory=lambda: None)

    await repo.list_by_ids_in_session(session, [uuid4()], lock=True)

    rendered = str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "FOR SHARE" in rendered
    # ...and NOT `FOR UPDATE`: concurrent workflow creations referencing the
    # same templates must not serialise against each other.
    assert "FOR UPDATE" not in rendered
