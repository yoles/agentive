"""``/api/v1/workflows`` — Workflow Engine endpoints (Story 4.1, 4.2).

* ``POST /workflows`` — create a workflow from a client-submitted DAG
  (``{name, nodes, edges}``). Validates structural integrity, branching
  conditions, and surfaces a non-blocking Contrôleur/Producteur LLM-diversity
  warning (FR15, D84). No DAG-builder UI exists — the body is built by the
  caller (Postman, tests, or a future client, out of scope here).
* ``POST /workflows/{workflow_id}/runs`` — start a run of an existing,
  active workflow (Story 4.2 AC1). Returns immediately — execution happens
  in a fire-and-forget background task.
* ``GET /workflows/runs/{run_id}/events`` — SSE stream of a run's state
  transitions (Story 4.2 AC1).

Sits behind ``AuthTokenMiddleware`` (Story 1.7). ``AgentiveError`` is raised
for domain failures and converted to RFC 7807 by the global handler in
``app.main``.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, Request, status
from sse_starlette.sse import EventSourceResponse

from agentive_backend.features.workflow_engine.schemas import (
    CreateWorkflowRequest,
    CreateWorkflowResponse,
    StartRunRequest,
    StartRunResponse,
)
from agentive_backend.features.workflow_engine.service import (
    WorkflowExecutionService,
    WorkflowService,
)
from agentive_backend.shared.event_bus import subscribe
from agentive_backend.shared.exceptions import DependencyError, NotFoundError
from agentive_backend.shared.logging import get_logger
from agentive_backend.shared.repositories import AgentTemplateRepo, WorkflowRepo, WorkflowRunRepo

if TYPE_CHECKING:
    from agentive_backend.infra.db.models import WorkflowRun
    from agentive_backend.shared.event_bus import Event

_log = get_logger(__name__)

router = APIRouter(tags=["workflows"])

# Matches every run lifecycle event published by `WorkflowExecutionService`/
# `WorkflowRecoveryWorker` — `started`, `step_completed`, `resumed`,
# `completed`, `failed` (T6). Subscriptions are process-wide (not scoped by
# run), so the handler filters on `payload["run_id"]` itself.
_RUN_EVENT_PATTERN = re.compile(r"workflow_engine\.workflow_run\.\w+")

# Terminal statuses / event-type suffixes — a stream never outlives these.
#
# The two vocabularies differ ON PURPOSE, and AC1's own wording ("`completed`,
# `error`") conflates them. `workflow_runs.status` uses `error`, and every
# `state` frame this endpoint emits carries that value verbatim. The SSE
# EVENT NAME stays `failed`, because `error` is a reserved event in the
# browser `EventSource` API: it is what fires on a transport-level failure,
# so a server-sent event of the same name would be indistinguishable from a
# dropped connection in `es.addEventListener("error", ...)`. Divergence from
# the AC letter, deliberate — cf Dev Notes.
_TERMINAL_STATUSES = frozenset({"completed", "error"})
_TERMINAL_EVENT_SUFFIXES = frozenset({"completed", "failed"})

# How long one queue-poll blocks before the loop re-checks its exit
# conditions (deadline, DB status re-poll).
_QUEUE_POLL_INTERVAL_S = 1.0

# WALL-CLOCK ceiling on a subscription, not an iteration count. The previous
# `for _ in range(3600)` was advertised as "~1h" but only holds when every
# iteration times out: an iteration that RECEIVES an event returns
# immediately, so a chatty run burned the whole budget in seconds — and then
# the generator returned with no terminal event and no error, leaving the
# client waiting forever on a stream that was already closed.
_MAX_STREAM_DURATION_S = 3600.0

# How often the loop re-reads `workflow_runs.status` while idle. The run's
# authoritative status lives in the row, not in the event stream (cf
# `WorkflowExecutionService`'s class docstring, which accepts that a
# lifecycle event may be dropped) — so the stream must not depend solely on
# receiving a terminal EVENT to learn that the run is over.
_STATUS_REPOLL_INTERVAL_S = 15.0

# Bounded per-client buffer. Unbounded, a client that stopped reading grew
# this queue for the entire run. `put_nowait` + drop (below) is what keeps a
# slow consumer from back-pressuring the shared event-bus dispatcher onto
# every OTHER subscriber; the `_STATUS_REPOLL_INTERVAL_S` poll is what makes
# a dropped event recoverable.
_EVENT_QUEUE_MAXSIZE = 256


def _build_workflow_service(request: Request) -> WorkflowService:
    """Return a :class:`WorkflowService` wired from ``app.state``.

    No ``archetype_registry`` needed here (unlike ``agent_registry``) — the
    archetype is read from the ``agent_templates.archetype`` DB column, not
    the in-process YAML registry.
    """
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise DependencyError(
            detail="Workflow engine not initialised — check lifespan startup logs.",
            context={"missing": ["session_factory"]},
        )
    return WorkflowService(
        workflow_repo=WorkflowRepo(session_factory=session_factory),
        template_repo=AgentTemplateRepo(session_factory=session_factory),
    )


def _build_execution_service(request: Request) -> WorkflowExecutionService:
    """Return the shared :class:`WorkflowExecutionService` from ``app.state``
    (Story 4.2 T9.3) — 503 if the lifespan hasn't wired it yet.

    The lifespan builds exactly one instance and shares it with the recovery
    worker; T9.3's comment already claimed this function read it. It did not:
    it rebuilt a fresh service and three fresh repos on EVERY request, while
    ``app.state.workflow_execution_service`` sat unread. Harmless in effect,
    but it meant the router and the recovery worker were quietly running on
    two different objects — so any future per-instance state (a cache, a
    semaphore, a concurrency cap) would have silently applied to only one of
    them.
    """
    # `app.state` is untyped (`Any`), hence the explicit isinstance narrowing
    # rather than a bare truthiness check.
    service = getattr(request.app.state, "workflow_execution_service", None)
    if isinstance(service, WorkflowExecutionService):
        return service

    # Not wired — report WHICH lifespan resource is missing rather than the
    # single opaque "service is None", since that is what an operator reading
    # the 503 needs in order to find the failed startup step.
    missing = [
        name
        for name in ("session_factory", "llm_router", "workflow_checkpointer")
        if getattr(request.app.state, name, None) is None
    ] or ["workflow_execution_service"]
    raise DependencyError(
        detail="Workflow execution engine not initialised — check lifespan startup logs.",
        context={"missing": missing},
    )


@router.post(
    "/workflows",
    response_model=CreateWorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workflow from a client-submitted DAG (Story 4.1)",
)
async def create_workflow(request: Request, body: CreateWorkflowRequest) -> CreateWorkflowResponse:
    """201 on success — ``warnings`` may be non-empty (never blocking, AC4).

    Errors :
    * 422 — duplicate ``node_id``, dangling edge, unknown ``agent_template_id``,
      cycle, invalid branching-condition syntax, or a condition referencing a
      variable not exposed by the emitting node's ``output_contract.core``
      (RFC 7807).
    * 503 — lifespan state missing (session factory).
    """
    service = _build_workflow_service(request)
    return await service.create_workflow(
        name=body.name,
        nodes=body.nodes,
        edges=body.edges,
        tenant_id=None,  # Story 4.1 anti-scope — single-tenant MVP.
    )


@router.post(
    "/workflows/{workflow_id}/runs",
    response_model=StartRunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a workflow run (Story 4.2 AC1)",
)
async def start_workflow_run(
    workflow_id: UUID, request: Request, body: StartRunRequest
) -> StartRunResponse:
    """201 returned IMMEDIATELY — execution runs in a background task, the
    request never blocks on the run's progress (AC1).

    Errors:
    * 404 — ``workflow_id`` unknown (URL's primary resource, unlike 4.1's
      ``POST /workflows`` where an invalid id lives in the body — cf Dev
      Notes § "404 vs 422").
    * 422 — the workflow exists but ``status != "active"``.
    * 503 — lifespan state missing (session factory / llm_router / checkpointer).
    """
    service = _build_execution_service(request)
    return await service.start_run(
        workflow_id=workflow_id,
        run_input=body.input,
        tenant_id=None,  # Story 4.2 anti-scope — single-tenant MVP.
    )


@router.get(
    "/workflows/runs/{run_id}/events",
    summary="Stream a run's state transitions via SSE (Story 4.2 AC1)",
)
async def stream_workflow_run_events(run_id: UUID, request: Request) -> EventSourceResponse:
    """Sends the current run state immediately on connect (catch-up for a
    run that already progressed), then live ``started``/``step_completed``/
    ``resumed``/``completed``/``failed`` events until the run reaches a
    terminal state or the client disconnects.

    Errors:
    * 404 — ``run_id`` unknown.
    * 503 — lifespan state missing (session factory).
    """
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise DependencyError(
            detail="Workflow engine not initialised — check lifespan startup logs.",
            context={"missing": ["session_factory"]},
        )
    workflow_run_repo = WorkflowRunRepo(session_factory=session_factory)
    run = await workflow_run_repo.get_by_id(run_id)
    if run is None:
        raise NotFoundError(
            detail=f"Workflow run '{run_id}' not found", context={"run_id": str(run_id)}
        )
    return EventSourceResponse(_stream_run_events(run, workflow_run_repo))


def _state_event(run: WorkflowRun, *, reason: str | None = None) -> dict[str, str]:
    """Build the ``state`` frame — AC1's catch-up payload.

    AC1 asks for "l'état courant [...] rattrapage si le run a déjà
    progressé", and a bare ``status`` does not say how far the run got: a
    client attaching to a run already on its fourth node received exactly
    the same frame as one attaching at node zero. The applicative checkpoint
    summary already holds the answer, so surface the two fields that
    describe progress, plus the failure reason for a run that is already
    terminal.

    Deliberately NOT forwarded: ``node_outputs_preview`` (up to 500 chars
    per node — this frame must stay small) and ``recovery_attempts`` (an
    internal bookkeeping counter, not run state).
    """
    checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
    payload: dict[str, Any] = {"run_id": str(run.id), "status": run.status}
    last_node_id = checkpoint.get("last_node_id")
    if isinstance(last_node_id, str):
        payload["last_node_id"] = last_node_id
    node_statuses = checkpoint.get("node_statuses")
    if isinstance(node_statuses, dict):
        payload["node_statuses"] = node_statuses
    last_error = checkpoint.get("last_error")
    if isinstance(last_error, str):
        # Already redacted at the source (`_mark_failed`), never re-derived here.
        payload["last_error"] = last_error
    if reason is not None:
        payload["reason"] = reason
    return {"event": "state", "data": json.dumps(payload, default=str)}


async def _stream_run_events(
    run: WorkflowRun, workflow_run_repo: WorkflowRunRepo
) -> AsyncIterator[dict[str, str]]:
    run_id = run.id

    if run.status in _TERMINAL_STATUSES:
        yield _state_event(run)
        return

    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)

    async def _handler(event: Event) -> None:
        if str(event.payload.get("run_id")) != str(run_id):
            return
        try:
            # NOT `await queue.put(...)`: this handler runs on the shared
            # event-bus dispatcher, so blocking here would make one
            # unresponsive SSE client stall delivery for every other
            # subscriber in the process. Dropping is safe because the
            # periodic status re-poll below reconciles the client anyway.
            queue.put_nowait(event)
        except asyncio.QueueFull:
            _log.warning(
                "workflow_engine.sse_event_dropped",
                run_id=str(run_id),
                event_type=event.event_type,
            )

    # Subscribe BEFORE re-checking status — closes the race window where the
    # run could transition between the route handler's initial load and here.
    sub = await subscribe(_RUN_EVENT_PATTERN, _handler)
    try:
        current = await workflow_run_repo.get_by_id(run_id)
        latest = current if current is not None else run
        yield _state_event(latest)
        if latest.status in _TERMINAL_STATUSES:
            return

        # NOT `request.is_disconnected()` here. That call re-issues a
        # `receive()` on the ASGI channel, which conflicts with
        # `sse-starlette`'s own consumption of it in some arrangements —
        # including under the pure-ASGI middlewares this story converted
        # `AuthTokenMiddleware`/`CorrelationIdMiddleware` to (an earlier
        # version of this comment blamed `BaseHTTPMiddleware`, which this
        # very story had already removed; the conflict is with
        # `sse-starlette`, not with the middleware base class). No polling
        # is needed: `sse-starlette` stops iterating this generator on a
        # real client disconnect, raising `GeneratorExit` — caught by
        # nothing here, so the `finally` still unsubscribes.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _MAX_STREAM_DURATION_S
        next_repoll = loop.time() + _STATUS_REPOLL_INTERVAL_S

        while loop.time() < deadline:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_QUEUE_POLL_INTERVAL_S)
            except TimeoutError:
                # Idle tick — the cheapest place to reconcile against the
                # row, which is the authoritative status. Without this, a
                # single dropped terminal event left the client hanging
                # until the deadline.
                if loop.time() >= next_repoll:
                    next_repoll = loop.time() + _STATUS_REPOLL_INTERVAL_S
                    current = await workflow_run_repo.get_by_id(run_id)
                    if current is not None and current.status in _TERMINAL_STATUSES:
                        yield _state_event(current, reason="status_repoll")
                        return
                continue
            action = event.event_type.rsplit(".", 1)[-1]
            yield {"event": action, "data": json.dumps(event.payload, default=str)}
            if action in _TERMINAL_EVENT_SUFFIXES:
                return

        # Deadline reached. Always close on a definitive frame rather than
        # just returning — a silent end is indistinguishable, client-side,
        # from a stream still waiting for the next event.
        current = await workflow_run_repo.get_by_id(run_id)
        if current is not None:
            yield _state_event(current, reason="stream_timeout")
        else:
            yield {
                "event": "state",
                "data": json.dumps(
                    {"run_id": str(run_id), "status": "unknown", "reason": "stream_timeout"}
                ),
            }
    finally:
        sub.unsubscribe()


__all__ = ["router"]
