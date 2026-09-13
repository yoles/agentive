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
    control_signal: str | None = None,
    control_requested_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id or uuid4(),
        status=status,
        checkpoint=checkpoint,
        control_signal=control_signal,
        control_requested_at=control_requested_at,
    )


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


@pytest.mark.asyncio
async def test_stream_reports_a_pause_whose_event_was_dropped(
    monkeypatch: pytest.MonkeyPatch, captured_handler: dict[str, Any]
) -> None:
    """Story 4.6, review lot 6 (P-K) — the worst case the terminal-only
    re-poll left open.

    `paused` is deliberately NOT terminal (the stream must survive a
    resume), and a paused run emits nothing further, so the queue never
    wakes the loop again. With the re-poll looking only for terminality, a
    dropped `paused` event — `put_nowait` drops by design under a slow
    consumer — left the client staring at "running" on a silent stream for
    the full hour."""
    monkeypatch.setattr(router_module, "_QUEUE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(router_module, "_STATUS_REPOLL_INTERVAL_S", 0.0)
    # Bounded deadline, and it is not decoration (review lot 10, T1). Without
    # it a regression yields NO second frame, so `_drain(limit=2)` never
    # reaches its limit and the generator loops until `_MAX_STREAM_DURATION_S`
    # — 3600 s. There is no `pytest-timeout` in `addopts`, so the result is a
    # one-hour CI stall with no diagnostic, not a failure. With it, the
    # regression closes on a `stream_timeout` frame and the assertion below
    # fails in under a second, saying exactly what went wrong.
    monkeypatch.setattr(router_module, "_max_stream_duration_s", lambda: 0.5)
    running = _run()
    paused = _run(status="paused", run_id=running.id, checkpoint={"last_node_id": "b"})

    frames = await _drain(_stream_run_events(running, _repo(running, paused)), limit=2)

    assert '"status": "paused"' in frames[-1]["data"]
    assert '"reason": "status_repoll"' in frames[-1]["data"]


@pytest.mark.asyncio
async def test_stream_reports_a_pending_control_signal_whose_event_was_dropped(
    monkeypatch: pytest.MonkeyPatch, captured_handler: dict[str, Any]
) -> None:
    """A pause REQUEST changes `control_signal` and nothing else — the row
    still says `running`. Reconciling on `status` alone would have missed
    it, which is precisely the window T5.5 exists to make visible."""
    monkeypatch.setattr(router_module, "_QUEUE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(router_module, "_STATUS_REPOLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(router_module, "_max_stream_duration_s", lambda: 0.5)  # cf T1 above
    running = _run()
    requested = _run(run_id=running.id, control_signal="pause")

    frames = await _drain(_stream_run_events(running, _repo(running, requested)), limit=2)

    assert '"control_signal": "pause"' in frames[-1]["data"]
    assert '"reason": "status_repoll"' in frames[-1]["data"]


@pytest.mark.asyncio
async def test_repoll_does_not_repeat_a_state_frame_that_has_not_changed(
    monkeypatch: pytest.MonkeyPatch, captured_handler: dict[str, Any]
) -> None:
    """Reconciling on CHANGE, not on every tick: an idle run polled twice a
    second must not turn the stream into a state-frame firehose."""
    monkeypatch.setattr(router_module, "_QUEUE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(router_module, "_STATUS_REPOLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(router_module, "_max_stream_duration_s", lambda: 0.08)
    run = _run()

    frames = await _drain(_stream_run_events(run, _repo(run)))

    # The opening frame and the deadline's closing frame — nothing in between.
    assert [f["event"] for f in frames] == ["state", "state"]
    assert '"reason": "stream_timeout"' in frames[-1]["data"]


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
    monkeypatch.setattr(router_module, "_max_stream_duration_s", lambda: 0.05)
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
    monkeypatch.setattr(router_module, "_max_stream_duration_s", lambda: 30.0)
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


# ─── Revue 2026-09-12 — le seul appel DB non gardé du chemin SSE ───────


@pytest.mark.asyncio
async def test_stream_survives_a_database_blip_during_the_repoll(
    monkeypatch: pytest.MonkeyPatch, captured_handler: dict[str, Any]
) -> None:
    """The loop's single `except` catches the BUILTIN `TimeoutError` around
    `queue.get()`. `sqlalchemy.exc.TimeoutError` — what pool exhaustion
    raises — is NOT a subclass of it, and neither is `OperationalError` from a
    failover. So a blip propagated out of the generator long after the
    response headers were sent: no RFC 7807 body is possible there, the client
    just saw the connection end — the silent end the deadline block declares
    unacceptable. And it was fleet-wide: every open stream re-polls on the
    same cadence, so one hiccup dropped all of them at once.
    """
    import sqlalchemy.exc

    # The premise, asserted rather than assumed.
    assert not issubclass(sqlalchemy.exc.TimeoutError, TimeoutError)

    monkeypatch.setattr(router_module, "_QUEUE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(router_module, "_STATUS_REPOLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(router_module, "_max_stream_duration_s", lambda: 0.2)
    run = _run()
    repo = _repo(run)
    calls = {"n": 0}

    async def _flaky(_run_id: UUID, **_kw: Any) -> Any:
        calls["n"] += 1
        if calls["n"] > 1:  # the opening read succeeds, every re-poll blips
            raise sqlalchemy.exc.TimeoutError("QueuePool limit reached")
        return run

    repo.get_by_id = AsyncMock(side_effect=_flaky)

    frames = await _drain(_stream_run_events(run, repo))

    # The stream stayed up and closed on a definitive frame of its own accord.
    assert frames[-1]["event"] == "state"
    assert '"reason": "stream_timeout"' in frames[-1]["data"]
    assert calls["n"] > 1  # the blip really was exercised


# ─── Story 4.9 AC6/T6.1 — `control_requested_at` observability ─────────


def test_state_event_carries_control_requested_at_alongside_the_signal() -> None:
    when = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    run = _run(control_signal="pause", control_requested_at=when)

    payload = _state_event(run)["data"]

    assert '"control_signal": "pause"' in payload
    assert "control_requested_at" in payload
    assert "2026-09-13" in payload


def test_state_event_omits_control_requested_at_when_nothing_is_pending() -> None:
    run = _run(control_signal=None, control_requested_at=None)

    payload = _state_event(run)["data"]

    assert "control_signal" not in payload
    assert "control_requested_at" not in payload


# ─── Story 4.9 AC6/T6.6 — SSE ceiling reconciled with the stale threshold ──


def test_max_stream_duration_stays_above_the_stale_threshold_at_every_legal_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact inversion this story closes: at the settings' own `le`
    ceilings, `derive_stale_threshold_s` used to exceed the hardcoded
    3600 s ceiling this function replaces — a client could receive
    `stream_timeout` on a run the recovery worker did not even consider
    orphaned yet."""
    from agentive_backend.features.workflow_engine.recovery import derive_stale_threshold_s

    worst_case_stale_threshold_s = derive_stale_threshold_s(
        base_delay_s=60.0,
        max_delay_s=300.0,
        escalation_timeout_s=60.0,
        handoff_summary_timeout_s=45.0,
    )
    # The premise: this is the inversion that motivated T6.6 in the first
    # place. If this stops being true (settings' `le` ceilings moved down),
    # the assertion below is still correct, just no longer load-bearing.
    assert worst_case_stale_threshold_s > 3600.0

    monkeypatch.setattr(router_module.settings, "workflow_retry_base_delay_s", 60.0)
    monkeypatch.setattr(router_module.settings, "workflow_retry_max_delay_s", 300.0)
    monkeypatch.setattr(router_module.settings, "routing_escalation_timeout_s", 60.0)
    monkeypatch.setattr(router_module.settings, "workflow_handoff_summary_timeout_s", 45.0)

    assert router_module._max_stream_duration_s() > worst_case_stale_threshold_s


def test_max_stream_duration_keeps_the_one_hour_floor_at_default_settings() -> None:
    """No regression for the common case — default settings still get the
    ~1h ceiling documented since Story 4.2."""
    assert router_module._max_stream_duration_s() >= 3600.0
