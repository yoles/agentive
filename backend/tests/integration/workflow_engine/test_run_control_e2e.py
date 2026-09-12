"""End-to-end integration test — run control (Story 4.6 T12.9, AC1/AC2).

Real Postgres, real ``AsyncPostgresSaver`` checkpointing, gated mock LLM (no
network). Exercises what no unit test can: that a pause observed at a
superstep boundary really does leave a resumable run, and that resuming it
does not re-execute a node LangGraph already committed.

The gated provider is the load-bearing fixture. Interruption is COOPERATIVE —
it takes effect at the end of a superstep — so a test that merely raced a
``pause`` against a fast run would be timing-dependent in both directions:
green when the pause lost, green when it won, and unable to tell which
happened. Blocking the LLM call on an ``asyncio.Event`` makes "the run is
mid-node right now" a fact rather than a hope (pattern introduced by
``test_start_run_returns_before_the_run_finishes``, Story 4.2 review #38).

⚠️ ``postgres_container`` is session-scoped: every test here MUST leave its
run in a settled state (``paused`` or terminal) before returning. A run left
``running`` is claimed by ``test_recovery_e2e.py``, which sweeps with
``stale_threshold_s=0.0`` — that pollution is intermittent, and it took a
Story 4.5 completion note to diagnose the last time it happened.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.llm.router import LLMRouter
from agentive_backend.shared.llm.types import Completion
from agentive_backend.shared.repositories import AgentTemplateRepo

from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app
from .conftest import wire_execution_service

pytestmark = pytest.mark.integration

_POLL_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.05


def _completion(text_: str) -> Completion:
    return Completion(
        text=text_,
        model="mock-model",
        provider="gated",
        input_tokens=10,
        output_tokens=5,
        finish_reason="stop",
        latency_ms=5.0,
        cost_estimate_usd=Decimal("0.0001"),
    )


# NOTE — pourquoi il n'y a PAS de `try/finally` autour des `provider.gate.set()`.
#
# Chaque test libère son gate APRÈS ses assertions, donc une assertion qui
# casse saute la libération. La revue (groupe 3) a signalé que la tâche
# `_drive_run` resterait alors bloquée sur `gate.wait()` et laisserait une row
# `running` à vie, empoisonnant `test_recovery_e2e.py` (qui balaie avec
# `stale_threshold_s=0.0` et réclame toute row encore `running`).
#
# Reproduit avant d'être corrigé : échec injecté avant la libération, puis
# `test_recovery_e2e.py` lancé derrière dans la même session. La cascade N'A
# PAS lieu — au démontage des fixtures la tâche bloquée se réveille sur une
# session fermée, `_mark_failed` s'exécute, et le run finit `error`, donc hors
# de portée du sweep. Un filet (registre + fixture autouse drainant les gates)
# a été écrit puis RETIRÉ : il ne changeait rien au comportement observé, et le
# réordonner devant le démontage de l'app cassait un second test.
#
# Ce qui reste, et qui est assumé : un test en échec laisse dans ses propres
# logs un event `workflow_run.failed` fantôme et un `OperationalError`
# déroutant. Du bruit dans un test déjà rouge, pas une contamination.


class _GatedProvider:
    """Blocks each completion until the test opens the gate, counting calls.

    ``call_count`` is what makes "no node was re-executed" and "the provider
    stopped being called" assertable rather than assumed.
    """

    provider_name = "gated"

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.call_count = 0
        #: Set every time a call ARRIVES, so a test can wait for the run to
        #: be genuinely inside a node instead of sleeping and hoping.
        self.entered = asyncio.Event()

    async def complete(self, messages: Any, **_kwargs: Any) -> Completion:
        self.call_count += 1
        self.entered.set()
        await self.gate.wait()
        return _completion(json.dumps({"call": self.call_count}))


async def _create_template(session_factory: async_sessionmaker[AsyncSession]) -> Any:
    repo = AgentTemplateRepo(session_factory=session_factory)
    return await repo.create(name=f"tpl-{uuid4()}", archetype="producteur", config={})


async def _poll_run(
    factory: async_sessionmaker[AsyncSession], run_id: str, *, until: set[str]
) -> dict[str, Any]:
    """Wait for ``run_id`` to reach one of ``until``.

    Accepts a SET of statuses — ``paused`` is a perfectly good stopping point
    for this story even though it is not terminal. Mirror
    ``test_execute_workflow_e2e._poll_run_status``, which already took the
    same parameter; deliberately not a second polling helper.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _POLL_TIMEOUT_S
    while loop.time() < deadline:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT status, control_signal, control_requested_at, ended_at, "
                    "checkpoint, metrics FROM workflow_runs WHERE id = :id"
                ),
                {"id": run_id},
            )
            row = result.mappings().one()
        if row["status"] in until:
            return dict(row)
        await asyncio.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"run {run_id} never reached {until} within {_POLL_TIMEOUT_S}s (last: {row})")


async def _count_outbox(
    factory: async_sessionmaker[AsyncSession], event_type: str, *, run_id: str
) -> int:
    async with factory() as session:
        result = await session.execute(
            text(
                "SELECT COUNT(*) FROM outbox_events "
                "WHERE event_type = :t AND payload->>'run_id' = :r"
            ),
            {"t": event_type, "r": run_id},
        )
        return int(result.scalar_one())


async def _setup(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    *,
    node_count: int = 2,
) -> tuple[Any, _GatedProvider]:
    """Build an app whose workflow is a linear chain of ``node_count`` gated
    nodes, so a pause between two supersteps is observable."""
    templates = [await _create_template(app_session_factory) for _ in range(node_count)]
    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    provider = _GatedProvider()
    app.state.llm_router = LLMRouter(providers={"gated": provider}, default_chain=["gated"])
    wire_execution_service(app)
    app.state._e2e_templates = templates
    return app, provider


async def _create_workflow(client: httpx.AsyncClient, app: Any, name: str) -> str:
    node_ids = [chr(ord("a") + i) for i in range(len(app.state._e2e_templates))]
    resp = await client.post(
        "/api/v1/workflows",
        headers=_auth_headers(),
        json={
            "name": f"{name}-{uuid4()}",
            "nodes": [
                {"node_id": nid, "agent_template_id": str(tpl.id)}
                for nid, tpl in zip(node_ids, app.state._e2e_templates, strict=True)
            ],
            "edges": [
                {"from_node_id": node_ids[i], "to_node_id": node_ids[i + 1]}
                for i in range(len(node_ids) - 1)
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["workflow_id"])


async def _start_run(client: httpx.AsyncClient, workflow_id: str) -> str:
    resp = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        headers=_auth_headers(),
        json={"input": {"q": "hello"}},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["run_id"])


@pytest.mark.asyncio
async def test_pause_when_run_is_mid_node_should_suspend_at_the_superstep_boundary(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC2 — the pause takes effect AFTER the node in flight, not during it,
    and the provider is never called again."""
    app, provider = await _setup(app_session_factory, workflow_checkpointer)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _create_workflow(client, app, "e2e-pause")
        run_id = await _start_run(client, workflow_id)

        # The run is now genuinely inside node `a`'s LLM call.
        await asyncio.wait_for(provider.entered.wait(), timeout=_POLL_TIMEOUT_S)

        pause_resp = await client.post(
            f"/api/v1/workflows/runs/{run_id}/pause", headers=_auth_headers()
        )
        # 202, NOT 200: recorded, not applied.
        assert pause_resp.status_code == 202, pause_resp.text
        assert pause_resp.json()["status"] == "running"
        assert pause_resp.json()["control_signal"] == "pause"

        # Let node `a` finish. The driver observes the signal at the boundary.
        provider.gate.set()
        row = await _poll_run(seed_session_factory, run_id, until={"paused"})
        calls_at_pause = provider.call_count

    assert row["status"] == "paused"
    # The signal was CONSUMED atomically with the transition — a leftover
    # would make the next resume pause itself again before running anything.
    assert row["control_signal"] is None
    assert row["control_requested_at"] is None
    # A paused run is suspended, not finished.
    assert row["ended_at"] is None
    # Node `a` landed; node `b` never started.
    assert row["checkpoint"]["last_node_id"] == "a"
    assert calls_at_pause == 1

    assert (
        await _count_outbox(
            seed_session_factory, "workflow_engine.workflow_run.pause_requested", run_id=run_id
        )
        == 1
    )
    assert (
        await _count_outbox(
            seed_session_factory, "workflow_engine.workflow_run.paused", run_id=run_id
        )
        == 1
    )
    assert (
        await _count_outbox(
            seed_session_factory, "workflow_engine.workflow_run.completed", run_id=run_id
        )
        == 0
    )


@pytest.mark.asyncio
async def test_resume_when_run_was_paused_should_complete_without_replaying_a_node(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC2's strongest claim, and the reason interruption is cooperative:
    resuming costs ONE more LLM call, not two. Mirror
    ``test_recovery_e2e.py::test_resume_after_sigkill_does_not_replay_node_a``.
    """
    app, provider = await _setup(app_session_factory, workflow_checkpointer)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _create_workflow(client, app, "e2e-resume")
        run_id = await _start_run(client, workflow_id)
        await asyncio.wait_for(provider.entered.wait(), timeout=_POLL_TIMEOUT_S)

        await client.post(f"/api/v1/workflows/runs/{run_id}/pause", headers=_auth_headers())
        provider.gate.set()
        await _poll_run(seed_session_factory, run_id, until={"paused"})
        assert provider.call_count == 1

        resume_resp = await client.post(
            f"/api/v1/workflows/runs/{run_id}/resume", headers=_auth_headers()
        )
        assert resume_resp.status_code == 202, resume_resp.text
        assert resume_resp.json()["status"] == "running"
        assert resume_resp.json()["control_signal"] is None

        row = await _poll_run(seed_session_factory, run_id, until={"completed", "error"})

    assert row["status"] == "completed", row
    # THE assertion. Two nodes, two LLM calls total — node `a` was restored
    # from LangGraph's committed checkpoint, never re-run.
    assert provider.call_count == 2
    assert row["checkpoint"]["node_statuses"] == {"a": "success", "b": "success"}
    assert (
        await _count_outbox(
            seed_session_factory, "workflow_engine.workflow_run.resumed", run_id=run_id
        )
        == 1
    )


@pytest.mark.asyncio
async def test_cancel_when_run_is_live_should_stop_it_and_keep_partial_metrics(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC2 — a cancelled run is terminal, stamped, and keeps what it spent."""
    app, provider = await _setup(app_session_factory, workflow_checkpointer)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _create_workflow(client, app, "e2e-cancel")
        run_id = await _start_run(client, workflow_id)
        await asyncio.wait_for(provider.entered.wait(), timeout=_POLL_TIMEOUT_S)

        cancel_resp = await client.post(
            f"/api/v1/workflows/runs/{run_id}/cancel", headers=_auth_headers()
        )
        assert cancel_resp.status_code == 202, cancel_resp.text
        assert cancel_resp.json()["control_signal"] == "cancel"

        provider.gate.set()
        row = await _poll_run(seed_session_factory, run_id, until={"cancelled"})
        calls = provider.call_count

    assert row["status"] == "cancelled"
    assert row["ended_at"] is not None
    assert row["control_signal"] is None
    assert calls == 1
    # Node `a` was billed — its spend must survive the cancellation (mirror
    # `_mark_failed`, never `_mark_completed`).
    assert row["metrics"]["total_tokens"] == {"input": 10, "output": 5}
    assert (
        await _count_outbox(
            seed_session_factory, "workflow_engine.workflow_run.cancelled", run_id=run_id
        )
        == 1
    )


@pytest.mark.asyncio
async def test_cancel_when_run_is_paused_should_be_terminal_immediately_with_200(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC1 — no driver is alive to observe a signal on a paused run, so the
    request applies on the spot. 200, not 202: the status code is the
    message about whether the effect has already happened."""
    app, provider = await _setup(app_session_factory, workflow_checkpointer)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _create_workflow(client, app, "e2e-cancel-paused")
        run_id = await _start_run(client, workflow_id)
        await asyncio.wait_for(provider.entered.wait(), timeout=_POLL_TIMEOUT_S)
        await client.post(f"/api/v1/workflows/runs/{run_id}/pause", headers=_auth_headers())
        provider.gate.set()
        await _poll_run(seed_session_factory, run_id, until={"paused"})

        cancel_resp = await client.post(
            f"/api/v1/workflows/runs/{run_id}/cancel", headers=_auth_headers()
        )
        assert cancel_resp.status_code == 200, cancel_resp.text
        body = cancel_resp.json()
        assert body["status"] == "cancelled"
        assert body["control_signal"] is None

        row = await _poll_run(seed_session_factory, run_id, until={"cancelled"})

    assert row["ended_at"] is not None
    assert (
        await _count_outbox(
            seed_session_factory, "workflow_engine.workflow_run.cancelled", run_id=run_id
        )
        == 1
    )


@pytest.mark.asyncio
async def test_control_when_transition_is_illegal_should_return_409_against_a_real_run(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC1's 409s, against a genuinely terminal row rather than a mock."""
    app, provider = await _setup(app_session_factory, workflow_checkpointer, node_count=1)
    provider.gate.set()  # never block — we want this run to finish

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _create_workflow(client, app, "e2e-409")
        run_id = await _start_run(client, workflow_id)
        await _poll_run(seed_session_factory, run_id, until={"completed", "error"})

        for action in ("pause", "resume", "cancel"):
            resp = await client.post(
                f"/api/v1/workflows/runs/{run_id}/{action}", headers=_auth_headers()
            )
            assert resp.status_code == 409, (action, resp.text)
            # `app.main`'s handler FLATTENS `exc.context` into RFC 7807
            # extension members rather than nesting it under a `context` key.
            # `current_status` is named that way for exactly this reason:
            # a plain `status` would collide with the reserved member and be
            # dropped, silently, with only a log line to say so.
            problem = resp.json()
            assert problem["status"] == 409
            assert problem["run_id"] == run_id
            assert problem["current_status"] == "completed"
            assert problem["allowed_from"]

        # A 409 must leave NO trace in the outbox — the discipline
        # `_mark_completed`/`_mark_failed` already enforce.
        for event_type in (
            "workflow_engine.workflow_run.pause_requested",
            "workflow_engine.workflow_run.cancel_requested",
            "workflow_engine.workflow_run.paused",
            "workflow_engine.workflow_run.cancelled",
        ):
            assert await _count_outbox(seed_session_factory, event_type, run_id=run_id) == 0

        missing = await client.post(
            f"/api/v1/workflows/runs/{uuid4()}/pause", headers=_auth_headers()
        )
        assert missing.status_code == 404, missing.text


@pytest.mark.asyncio
async def test_sse_stream_when_run_is_cancelled_should_close(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """T5.4 — the test that would have caught the silent regression.

    ``_TERMINAL_STATUSES`` used to be a local literal missing ``cancelled``,
    so an SSE client watching a cancelled run stayed connected until the
    one-hour ``_MAX_STREAM_DURATION_S`` ceiling, receiving nothing.
    """
    app, provider = await _setup(app_session_factory, workflow_checkpointer, node_count=1)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _create_workflow(client, app, "e2e-sse-cancel")
        run_id = await _start_run(client, workflow_id)
        await asyncio.wait_for(provider.entered.wait(), timeout=_POLL_TIMEOUT_S)
        await client.post(f"/api/v1/workflows/runs/{run_id}/cancel", headers=_auth_headers())
        provider.gate.set()
        await _poll_run(seed_session_factory, run_id, until={"cancelled"})

        # Attaching AFTER the run is terminal: the stream must emit the
        # catch-up `state` frame and close, not hang.
        async def _read_stream() -> list[str]:
            frames: list[str] = []
            async with client.stream(
                "GET",
                f"/api/v1/workflows/runs/{run_id}/events",
                headers=_auth_headers(),
            ) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    frames.append(line)
            return frames

        frames = await asyncio.wait_for(_read_stream(), timeout=_POLL_TIMEOUT_S)

    payloads = [line for line in frames if line.startswith("data:")]
    assert payloads, frames
    assert any('"status": "cancelled"' in line for line in payloads), payloads
