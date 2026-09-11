"""Unit tests — the SSE run-events generator (Story 4.2 AC1, review lot 2).

``test_sse_stream_e2e.py`` covers the happy path against a real Postgres +
``OutboxWorker``. It cannot reach the paths that matter most here — a
dropped event, an exhausted deadline, a client attaching mid-run — because
each needs either a clock the test controls or a queue it can overfill.
Those live here, driving ``_stream_run_events`` directly with a fake repo.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from agentive_backend.features.workflow_engine.router import (
    _RUN_EVENT_PATTERN,
    _TERMINAL_EVENT_SUFFIXES,
    _state_event,
    _stream_run_events,
)
from agentive_backend.shared.event_bus import Event

# NOT `import ...workflow_engine.router as router_module`: the package's
# `__init__` re-exports the `router` APIRouter instance, which shadows the
# submodule attribute — the import would bind the APIRouter, not the module.
router_module = importlib.import_module("agentive_backend.features.workflow_engine.router")


def _run(
    *,
    status: str = "running",
    checkpoint: dict[str, Any] | None = None,
    run_id: UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(id=run_id or uuid4(), status=status, checkpoint=checkpoint)


def _event(run_id: UUID, action: str, **payload: Any) -> Event:
    return Event(
        id=uuid4(),
        event_type=f"workflow_engine.workflow_run.{action}",
        payload={"run_id": str(run_id), **payload},
        correlation_id=uuid4(),
        created_at=datetime.now(UTC),
    )


class _FakeSubscription:
    def __init__(self) -> None:
        self.unsubscribed = False

    def unsubscribe(self) -> None:
        self.unsubscribed = True


@pytest.fixture
def captured_handler(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``subscribe`` so the test holds the handler the generator
    registered, and can push events into it by hand."""
    captured: dict[str, Any] = {"handler": None, "sub": _FakeSubscription()}

    async def _subscribe(_pattern: Any, handler: Any) -> _FakeSubscription:
        captured["handler"] = handler
        return captured["sub"]

    monkeypatch.setattr(router_module, "subscribe", _subscribe)
    return captured


def _repo(*rows: Any) -> AsyncMock:
    """A repo whose successive ``get_by_id`` calls return ``rows`` in order,
    then repeat the last one."""
    repo = AsyncMock()
    sequence = list(rows)

    async def _get_by_id(_run_id: UUID, **_kw: Any) -> Any:
        return sequence.pop(0) if len(sequence) > 1 else sequence[0]

    repo.get_by_id = AsyncMock(side_effect=_get_by_id)
    return repo


async def _drain(stream: AsyncIterator[dict[str, str]], limit: int = 20) -> list[dict[str, str]]:
    frames: list[dict[str, str]] = []
    async for frame in stream:
        frames.append(frame)
        if len(frames) >= limit:
            break
    return frames


# ─── #18 — catch-up payload ────────────────────────────────────────────


def test_state_event_carries_progress_for_a_run_already_underway() -> None:
    """AC1 asks for a catch-up "si le run a déjà progressé". A bare status
    gave a client attaching at node four exactly the same frame as one
    attaching at node zero."""
    run = _run(
        checkpoint={
            "last_node_id": "c",
            "node_statuses": {"a": "success", "b": "success", "c": "success"},
            "node_outputs_preview": {"a": "x" * 500},
        }
    )

    payload = _state_event(run)["data"]

    assert '"last_node_id": "c"' in payload
    assert '"node_statuses"' in payload
    # Bounded frame — the previews are deliberately not forwarded.
    assert "node_outputs_preview" not in payload


def test_state_event_forwards_the_redacted_failure_reason() -> None:
    """A client attaching to an already-failed run must learn WHY, not just
    that the status is `error`."""
    run = _run(status="error", checkpoint={"last_error": "provider timeout"})
    assert "provider timeout" in _state_event(run)["data"]


def test_state_event_tolerates_a_missing_or_malformed_checkpoint() -> None:
    for checkpoint in (None, {}, {"last_node_id": 42, "node_statuses": "nope"}):
        payload = _state_event(_run(checkpoint=checkpoint))["data"]  # type: ignore[arg-type]
        assert '"status": "running"' in payload


@pytest.mark.asyncio
async def test_terminal_run_yields_one_state_frame_and_stops(
    captured_handler: dict[str, Any],
) -> None:
    run = _run(status="completed", checkpoint={"last_node_id": "b"})
    frames = await _drain(_stream_run_events(run, _repo(run)))

    assert len(frames) == 1
    assert frames[0]["event"] == "state"


# ─── #19 — bounded queue ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_drops_instead_of_blocking_when_the_queue_is_full(
    monkeypatch: pytest.MonkeyPatch, captured_handler: dict[str, Any]
) -> None:
    """An unbounded queue grew for the whole run behind a client that
    stopped reading. Bounded, the handler must still never block: it runs on
    the shared dispatcher, so `await queue.put()` on a full queue would
    stall delivery for every OTHER subscriber in the process."""
    monkeypatch.setattr(router_module, "_EVENT_QUEUE_MAXSIZE", 2)
    run = _run()
    stream = _stream_run_events(run, _repo(run))
    await anext(stream)  # initial `state` frame — registers the handler

    handler = captured_handler["handler"]
    for _ in range(50):
        # Must complete without blocking and without raising.
        await handler(_event(run.id, "step_completed", node_id="a", duration_ms=1))

    await stream.aclose()


@pytest.mark.asyncio
async def test_handler_ignores_events_for_other_runs(
    captured_handler: dict[str, Any],
) -> None:
    run = _run()
    stream = _stream_run_events(run, _repo(run))
    await anext(stream)

    await captured_handler["handler"](_event(uuid4(), "completed"))

    await stream.aclose()


# ─── #17 — the row is the authoritative status ─────────────────────────


@pytest.mark.asyncio
async def test_stream_closes_on_repoll_when_the_terminal_event_never_arrives(
    monkeypatch: pytest.MonkeyPatch, captured_handler: dict[str, Any]
) -> None:
    """Lifecycle events are droppable by design (cf `WorkflowExecutionService`'s
    class docstring). Before this, a single dropped `completed` left the
    client hanging on an open stream until the deadline."""
    monkeypatch.setattr(router_module, "_QUEUE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(router_module, "_STATUS_REPOLL_INTERVAL_S", 0.0)
    running = _run()
    finished = _run(status="completed", run_id=running.id, checkpoint={"last_node_id": "z"})

    frames = await _drain(_stream_run_events(running, _repo(running, finished)))

    assert [f["event"] for f in frames] == ["state", "state"]
    assert '"status": "completed"' in frames[-1]["data"]
    assert '"reason": "status_repoll"' in frames[-1]["data"]


# ─── #16 — wall-clock deadline, and a definitive last frame ────────────


@pytest.mark.asyncio
async def test_deadline_closes_the_stream_with_a_final_state_frame(
    monkeypatch: pytest.MonkeyPatch, captured_handler: dict[str, Any]
) -> None:
    """The old cap counted ITERATIONS: an iteration that received an event
    returned instantly, so a chatty run burned the advertised "~1h" budget
    in seconds — and then the generator just returned, with no terminal
    event and no error, indistinguishable client-side from a stream still
    waiting."""
    monkeypatch.setattr(router_module, "_QUEUE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(router_module, "_STATUS_REPOLL_INTERVAL_S", 3600.0)
    monkeypatch.setattr(router_module, "_MAX_STREAM_DURATION_S", 0.05)
    run = _run()

    frames = await _drain(_stream_run_events(run, _repo(run)))

    assert frames[-1]["event"] == "state"
    assert '"reason": "stream_timeout"' in frames[-1]["data"]


@pytest.mark.asyncio
async def test_a_busy_stream_is_not_cut_short_by_event_volume(
    monkeypatch: pytest.MonkeyPatch, captured_handler: dict[str, Any]
) -> None:
    """Event volume must not consume the stream's budget.

    300 events spend 300 of the old iteration counter's 3600 tokens but
    essentially none of the wall-clock deadline — that is the whole
    difference between the two ceilings. This guards the delivery half of
    it: every event reaches the client and the terminal one still closes the
    stream. The timing half is covered by the deadline test above.
    """
    monkeypatch.setattr(router_module, "_QUEUE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(router_module, "_MAX_STREAM_DURATION_S", 30.0)
    # Bigger than the burst — this test is about the deadline, not about the
    # drop policy (which `test_handler_drops_...` covers on its own).
    monkeypatch.setattr(router_module, "_EVENT_QUEUE_MAXSIZE", 512)
    run = _run()
    stream = _stream_run_events(run, _repo(run))
    await anext(stream)

    handler = captured_handler["handler"]
    for index in range(300):
        await handler(_event(run.id, "step_completed", node_id=f"n{index}", duration_ms=1))
    await handler(_event(run.id, "failed", failed_node_id="n299", error_summary="boom"))

    frames = await _drain(stream, limit=400)

    assert frames[-1]["event"] == "failed"
    assert len(frames) == 301


# ─── #20 — status vocabulary vs event-name vocabulary ──────────────────


@pytest.mark.asyncio
async def test_failure_uses_the_error_status_but_the_failed_event_name(
    captured_handler: dict[str, Any],
) -> None:
    """The two vocabularies differ on purpose: `error` is reserved by the
    browser `EventSource` API for transport failures, so the SSE event name
    stays `failed` while the run STATUS reported in `state` frames is
    `error` — which is what AC1 actually names."""
    run = _run(status="error", checkpoint={"last_error": "boom"})
    frames = await _drain(_stream_run_events(run, _repo(run)))

    assert frames[0]["event"] == "state"
    assert '"status": "error"' in frames[0]["data"]


@pytest.mark.asyncio
async def test_subscription_is_released_even_when_the_client_walks_away(
    monkeypatch: pytest.MonkeyPatch, captured_handler: dict[str, Any]
) -> None:
    """`sse-starlette` closes this generator on a real disconnect, raising
    `GeneratorExit` — the `finally` must still unsubscribe, or every
    abandoned stream leaks a process-wide subscription."""
    monkeypatch.setattr(router_module, "_QUEUE_POLL_INTERVAL_S", 0.01)
    run = _run()
    stream = _stream_run_events(run, _repo(run))
    await anext(stream)

    await stream.aclose()

    assert captured_handler["sub"].unsubscribed is True


# ─── Story 4.3 T7.4 — routing_escalated must not close the stream ───────


def test_routing_escalated_is_not_a_terminal_event_suffix() -> None:
    """A new `workflow_engine.workflow_run.*` event that DID get added to
    `_TERMINAL_EVENT_SUFFIXES` would close the SSE stream at the first
    escalation instead of just forwarding the frame."""
    assert "routing_escalated" not in _TERMINAL_EVENT_SUFFIXES


def test_routing_escalated_event_type_matches_the_run_event_pattern() -> None:
    """It must still match the subscription pattern so it reaches the SSE
    client at all (T7's whole point: streamed "for free")."""
    assert _RUN_EVENT_PATTERN.fullmatch("workflow_engine.workflow_run.routing_escalated")
