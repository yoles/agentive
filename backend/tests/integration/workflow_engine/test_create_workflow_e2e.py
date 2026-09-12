"""End-to-end integration tests — ``POST /api/v1/workflows`` (Story 4.1 T8.6).

Agent templates are seeded directly via ``AgentTemplateRepo`` (not through
the HTTP API) — mirrors ``tests/integration/memory_manager/test_api.py``'s
approach for namespaces, since only the archetype + config shape matter
here, not the archetype-registry skeleton.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.exceptions import DependencyError
from agentive_backend.shared.repositories import AgentTemplateRepo, WorkflowRepo

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


# ─── Story 4.8 AC1 — idempotence on replay ─────────────────────────────


def _dag_body(name: str, tpl_a: Any, tpl_b: Any) -> dict[str, Any]:
    return {
        "name": name,
        "nodes": [
            {"node_id": "a", "agent_template_id": str(tpl_a.id)},
            {"node_id": "b", "agent_template_id": str(tpl_b.id)},
        ],
        "edges": [{"from_node_id": "a", "to_node_id": "b"}],
    }


async def _count_workflows(factory: async_sessionmaker[AsyncSession], *, name: str) -> int:
    async with factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM workflows WHERE name = :n"), {"n": name}
        )
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_create_workflow_replayed_body_creates_one_row_and_one_event(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Story 4.8 AC1 — the whole point of the story, end to end on real
    Postgres: the same body posted twice yields 201 then 200, the SAME
    workflow_id, exactly one row and exactly one outbox event."""
    tpl_a = await _create_template(app_session_factory)
    tpl_b = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)
    body = _dag_body("replayed-pipeline", tpl_a, tpl_b)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/v1/workflows", headers=_auth_headers(), json=body)
        second = await client.post("/api/v1/workflows", headers=_auth_headers(), json=body)

    assert first.status_code == 201, first.text
    assert first.json()["idempotent_replay"] is False

    # 200, not 201: nothing was created. The status code is what tells a
    # client which of the two happened without reading the body.
    assert second.status_code == 200, second.text
    assert second.json()["idempotent_replay"] is True
    assert second.json()["workflow_id"] == first.json()["workflow_id"]
    assert second.json()["version"] == first.json()["version"]

    assert await _count_workflows(seed_session_factory, name="replayed-pipeline") == 1
    assert (
        await _count_outbox(
            seed_session_factory,
            "workflow_engine.workflow.created",
            workflow_id=first.json()["workflow_id"],
        )
        == 1
    )


@pytest.mark.asyncio
async def test_create_workflow_same_dag_under_a_different_name_is_a_new_workflow(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 — the fingerprint covers `{name, dag}`, so the name remains the
    escape hatch for deliberately creating a twin pipeline."""
    tpl_a = await _create_template(app_session_factory)
    tpl_b = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/workflows", headers=_auth_headers(), json=_dag_body("twin-one", tpl_a, tpl_b)
        )
        second = await client.post(
            "/api/v1/workflows", headers=_auth_headers(), json=_dag_body("twin-two", tpl_a, tpl_b)
        )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["idempotent_replay"] is False
    assert second.json()["workflow_id"] != first.json()["workflow_id"]


@pytest.mark.asyncio
async def test_create_workflow_same_name_with_a_different_dag_is_a_new_workflow(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 — and the converse: the uniqueness rule is on the PAIR, so it is
    not a rename of `uq_agent_template`'s name-based constraint."""
    tpl_a = await _create_template(app_session_factory)
    tpl_b = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)
    solo = {
        "name": "same-name",
        "nodes": [{"node_id": "a", "agent_template_id": str(tpl_a.id)}],
        "edges": [],
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/v1/workflows", headers=_auth_headers(), json=solo)
        second = await client.post(
            "/api/v1/workflows", headers=_auth_headers(), json=_dag_body("same-name", tpl_a, tpl_b)
        )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["workflow_id"] != first.json()["workflow_id"]
    assert await _count_workflows(seed_session_factory, name="same-name") == 2


@pytest.mark.asyncio
async def test_create_workflow_two_concurrent_identical_requests_converge(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Story 4.8 AC1 — the ONLY test that distinguishes the mechanism that was
    built from the one that was rejected.

    A sequential replay (the test above) passes just as well with a naive
    pre-`SELECT` dedup. Two CONCURRENT identical requests do not: under
    `READ COMMITTED` neither transaction sees the other's uncommitted row, so
    a pre-`SELECT` would let both INSERT. Only insert-first + unique-index
    collision converges — which is why the code does that.

    Neither ordering is presumed: whichever request loses the race is the one
    that answers 200.
    """
    tpl_a = await _create_template(app_session_factory)
    tpl_b = await _create_template(app_session_factory)
    app = _make_app(session_factory=app_session_factory)
    body = _dag_body("concurrent-pipeline", tpl_a, tpl_b)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        left, right = await asyncio.gather(
            client.post("/api/v1/workflows", headers=_auth_headers(), json=body),
            client.post("/api/v1/workflows", headers=_auth_headers(), json=body),
        )

    assert sorted([left.status_code, right.status_code]) == [200, 201], (
        f"{left.status_code}/{left.text} vs {right.status_code}/{right.text}"
    )
    assert left.json()["workflow_id"] == right.json()["workflow_id"]
    assert [left.json()["idempotent_replay"], right.json()["idempotent_replay"]].count(True) == 1

    assert await _count_workflows(seed_session_factory, name="concurrent-pipeline") == 1
    assert (
        await _count_outbox(
            seed_session_factory,
            "workflow_engine.workflow.created",
            workflow_id=left.json()["workflow_id"],
        )
        == 1
    )


@pytest.mark.asyncio
async def test_create_workflow_losing_writer_fails_fast_on_a_stalled_winner(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Story 4.14 AC2 — the wait on ``uq_workflow_request_fingerprint`` must
    be BOUNDED, not survive a winner whose transaction never commits.

    Before this story, a losing writer blocked on the collision until the
    winning transaction's `FOR SHARE`-holding transaction ended — with no
    cap. This test manufactures exactly that stall deterministically (a
    session that inserts the colliding row and is never committed for the
    lifetime of the test) rather than relying on natural race timing, and
    proves the loser gives up with a TYPED, retriable error
    (:class:`DependencyError`, 503) once its own bounded wait expires —
    not a raw driver exception, and not an indefinite hang.

    ``lock_timeout_ms`` is set short (200ms) so the test itself stays fast;
    the mechanism under test is `WorkflowRepo.with_tenant`'s
    ``lock_timeout_ms=`` parameter and `create_in_session`'s translation of
    the resulting ``OperationalError`` — the exact pair `create_workflow`
    wires together in production, exercised here directly against the repo
    rather than through the HTTP layer, since what matters is bounded above
    the SQL, not the route.
    """
    fingerprint = "e" * 64
    repo = WorkflowRepo(session_factory=app_session_factory)
    dag_payload = {"nodes": [], "edges": []}

    # Session A — the stalled "winner": inserts the colliding fingerprint
    # and holds its transaction open across the whole test body, exactly
    # mirroring a winning `create_workflow` transaction that has not yet
    # committed.
    async with app_session_factory() as winner_session:
        await repo.create_in_session(
            winner_session,
            name="lock-timeout-winner",
            dag=dag_payload,
            request_fingerprint=fingerprint,
        )
        # Deliberately NOT committed — `winner_session` stays open, and so
        # does its row lock, until this `async with` block exits below.

        # Session B — the loser: same fingerprint, same tenant (`None`),
        # bounded to a short wait.
        with pytest.raises(DependencyError) as exc_info:
            async with asyncio.timeout(5.0):  # outer safety net, not the mechanism under test
                async with repo.with_tenant(None, lock_timeout_ms=200) as loser_session:
                    await repo.create_in_session(
                        loser_session,
                        name="lock-timeout-loser",
                        dag=dag_payload,
                        request_fingerprint=fingerprint,
                    )

        assert exc_info.value.status == 503
        assert exc_info.value.context["name"] == "lock-timeout-loser"

        await winner_session.rollback()

    # The winner's rollback releases the lock; a fresh attempt with no
    # contention must succeed normally — proves the timeout path did not
    # leave the fingerprint index or the repo in a broken state.
    created = await repo.create(
        name="lock-timeout-followup", dag=dag_payload, request_fingerprint=fingerprint
    )
    assert created.request_fingerprint == fingerprint
